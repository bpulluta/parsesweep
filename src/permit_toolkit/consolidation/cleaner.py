"""Data cleaning module for permit toolkit - removes zero-generator files and identifies versions."""

import json
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict


class ExtractionCleaner:
    """Cleans and organizes extracted permit data."""
    
    def __init__(self, extracted_dir: Path, output_dir: Path, dedup_strategy: str = "most_recent_highest_capacity"):
        self.extracted_dir = Path(extracted_dir)
        self.output_dir = Path(output_dir)
        self.dedup_strategy = dedup_strategy
        
        # Output subdirectories (no raw copy - source is already raw)
        self.cleaned_dir = self.output_dir
        self.reports_dir = self.output_dir / "reports"
        
        # Stats tracking
        self.stats = {
            "total_files": 0,
            "zero_generator_files": 0,
            "permit_version_groups": 0,
            "files_with_generators": 0,
            "final_cleaned_files": 0,
            "true_duplicates_found": 0,
            "duplicate_files_removed": 0,
            "facilities_before_dedup": 0,
            "facilities_after_dedup": 0,
        }
        
        # Tracking for reports
        self.zero_gen_files = []
        self.permit_versions = []
        self.duplicate_groups = []
        self.errors = []
    
    def setup_directories(self):
        """Create output directory structure."""
        self.cleaned_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
    
    def load_extraction(self, filepath: Path) -> dict:
        """Load and parse an extraction JSON file."""
        try:
            with open(filepath, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.errors.append({
                "file": str(filepath),
                "error": str(e),
                "stage": "loading"
            })
            return None
    
    def collect_all_files(self) -> dict[str, list[dict]]:
        """
        Collect all extraction files grouped by permit number.
        Returns: {permit_number: [file_info_dicts]}
        """
        permit_groups = defaultdict(list)
        
        for state_dir in self.extracted_dir.iterdir():
            if not state_dir.is_dir():
                continue
                
            for json_file in state_dir.glob("*.json"):
                data = self.load_extraction(json_file)
                if data is None:
                    continue
                
                self.stats["total_files"] += 1
                
                permit_number = data.get("permit_number", "UNKNOWN")
                generator_count = data.get("generator_count", 0)
                
                file_info = {
                    "path": json_file,
                    "relative_path": json_file.relative_to(self.extracted_dir),
                    "state": state_dir.name,
                    "permit_number": permit_number,
                    "generator_count": generator_count,
                    "completeness": data.get("completeness_score", 0.0),
                    "source_file": data.get("source_file", ""),
                    "data": data,
                }
                
                # Track zero generator files
                if generator_count == 0:
                    self.zero_gen_files.append(file_info)
                    self.stats["zero_generator_files"] += 1
                
                permit_groups[permit_number].append(file_info)
        
        return permit_groups
    
    def identify_permit_versions(
        self, permit_groups: dict[str, list[dict]], deduplicate: bool = False
    ) -> list[dict]:
        """
        Identify permits with multiple versions.
        We preserve all versions since they may contain different generators.
        
        Args:
            permit_groups: Dictionary of permit_number -> list of file_info dicts
            deduplicate: If True, detect and mark true duplicates for removal
        """
        version_reports = []
        
        for permit_number, files in permit_groups.items():
            if len(files) > 1:
                self.stats["permit_version_groups"] += 1
                
                # Sort by issue date if available, then by generator count
                sorted_files = sorted(
                    files,
                    key=lambda x: (
                        x["data"].get("data", {}).get(
                            "permitDetails", {}
                        ).get("permitIssuanceDate") or "",
                        x["generator_count"]
                    ),
                    reverse=True
                )
                
                # Check if these are true duplicates (same generators)
                is_duplicate = False
                duplicate_reason = None
                files_to_keep = sorted_files  # Default: keep all
                
                if deduplicate and len(files) >= 2:
                    # Compare generator reference numbers
                    is_duplicate, duplicate_reason, files_to_keep = (
                        self._check_true_duplicate(sorted_files)
                    )
                
                version_reports.append({
                    "permit_number": permit_number,
                    "version_count": len(files),
                    "state": files[0]["state"],
                    "is_true_duplicate": is_duplicate,
                    "duplicate_reason": duplicate_reason,
                    "files_to_keep": [f["path"].name for f in files_to_keep] if deduplicate else None,
                    "versions": [
                        {
                            "file": f["path"].name,
                            "source_pdf": f["source_file"],
                            "generators": f["generator_count"],
                            "completeness": f["completeness"],
                            "issue_date": f["data"].get("data", {}).get(
                                "permitDetails", {}
                            ).get("permitIssuanceDate", "N/A"),
                            "cost_usd": f["data"].get("cost_usd", 0),
                            "kept": f in files_to_keep if deduplicate else True
                        }
                        for f in sorted_files
                    ],
                    "total_generators_across_versions": sum(
                        f["generator_count"] for f in files
                    ),
                    "recommendation": "All versions preserved - may represent amendments or different extractions"
                })
        
        return version_reports
    
    def _check_true_duplicate(
        self, files: list[dict]
    ) -> tuple[bool, str | None, list[dict]]:
        """
        Check if multiple files are true duplicates (same generators).
        
        Returns:
            (is_duplicate, reason, files_to_keep)
        """
        if len(files) < 2:
            return False, None, files
        
        # Get generator reference numbers from each file
        gen_ref_sets = []
        for file_info in files:
            gen_sets = file_info["data"].get("data", {}).get("generatorSets", [])
            refs = set(
                gs.get("referenceNumber", "")
                for gs in gen_sets
                if gs.get("referenceNumber")
            )
            gen_ref_sets.append(refs)
        
        # Check if all files have the same generator references
        if len(gen_ref_sets) < 2:
            return False, None, files
        
        first_set = gen_ref_sets[0]
        all_same = all(refs == first_set for refs in gen_ref_sets[1:])
        
        if all_same and len(first_set) > 0:
            # True duplicate - keep only the newest (first in sorted list)
            reason = f"All {len(files)} versions have identical {len(first_set)} generator reference numbers"
            return True, reason, [files[0]]
        
        # Different generators - keep all
        return False, None, files
    
    def apply_smart_deduplication(
        self, permit_groups: dict[str, list[dict]]
    ) -> dict[str, dict]:
        """
        Apply smart deduplication using PermitDeduplicator.
        
        This considers:
        - Facility ID matches
        - Address similarity (fuzzy matching for OCR errors)
        - Permit dates (keep most recent)
        - Generator counts and capacity
        
        Args:
            permit_groups: Dictionary of permit_number -> list of file_info dicts
            
        Returns:
            Dictionary mapping file paths to file info for files to keep
        """
        from ..data.deduplicator import PermitDeduplicator
        
        # Convert file_info dicts to permit dicts for deduplicator
        all_permits = []
        file_info_map = {}  # Map permit data back to file_info
        
        for permit_number, files in permit_groups.items():
            for file_info in files:
                if file_info["generator_count"] == 0:
                    continue  # Skip zero-generator files
                
                # Extract data needed for deduplication
                permit_data = file_info["data"]
                permit_details = permit_data.get("data", {}).get("permitDetails", {})
                
                permit_dict = {
                    "permit_number": permit_number,
                    "facility_name": permit_details.get("facilityName", "Unknown"),
                    "facility_id": permit_details.get("stateFacilityID", ""),
                    "address_raw": permit_details.get("facilityAddress", ""),
                    "county": permit_details.get("facilityCounty", ""),
                    "state": file_info["state"],
                    "generator_count": file_info["generator_count"],
                    "permit_date": permit_details.get("permitIssuanceDate", ""),
                    "permit_expiration": permit_details.get("permitExpirationDate", None),
                    "total_capacity_kw": sum(
                        (gen.get("ratedCapacityKW") or 0) * (gen.get("numGenerators") or 1)
                        for gen in permit_data.get("data", {}).get("generatorSets", [])
                        if gen.get("ratedCapacityKW")
                    ),
                    "source_file": file_info["source_file"],
                    "_file_path": str(file_info["path"]),  # Keep track of file path
                }
                
                all_permits.append(permit_dict)
                file_info_map[str(file_info["path"])] = file_info
        
        self.stats["facilities_before_dedup"] = len(all_permits)
        
        # Apply deduplication
        deduplicator = PermitDeduplicator(strategy=self.dedup_strategy)
        deduplicated_permits = deduplicator.deduplicate_permits(all_permits)
        
        self.stats["facilities_after_dedup"] = len(deduplicated_permits)
        
        # Generate deduplication report
        dedup_report = deduplicator.get_deduplication_report(
            all_permits, deduplicated_permits
        )
        
        # Track which files were removed
        kept_paths = {p["_file_path"] for p in deduplicated_permits}
        removed_permits = [p for p in all_permits if p["_file_path"] not in kept_paths]
        
        # Store duplicate groups for reporting
        self.duplicate_groups = []
        for removed in removed_permits:
            # Find which kept permit this was a duplicate of
            for kept in deduplicated_permits:
                dedup = deduplicator.are_same_facility(removed, kept)
                if dedup:
                    self.duplicate_groups.append({
                        "removed_file": Path(removed["_file_path"]).name,
                        "kept_file": Path(kept["_file_path"]).name,
                        "facility_name": removed["facility_name"],
                        "address": removed["address_raw"][:60],
                        "reason": f"Same facility - kept {self.dedup_strategy}",
                    })
                    break
        
        # Return file_info dicts for files to keep
        files_to_keep = {}
        for permit in deduplicated_permits:
            file_path = permit["_file_path"]
            files_to_keep[file_path] = file_info_map[file_path]
        
        return files_to_keep, dedup_report
    
    def create_cleaned_dataset(
        self, permit_groups: dict, deduplicate: bool = False, smart_dedup: bool = False
    ):
        """Create cleaned dataset with ALL files that have generators."""
        cleaned_count = 0
        files_to_keep = set()
        versioned_permits = set()
        
        # Option 1: Smart deduplication (new sophisticated approach)
        if smart_dedup:
            files_to_keep_dict, dedup_report = self.apply_smart_deduplication(permit_groups)
            
            # Copy files that passed deduplication
            for file_path, file_info in files_to_keep_dict.items():
                src = Path(file_path)
                dest_state_dir = self.cleaned_dir / file_info["state"]
                dest_state_dir.mkdir(exist_ok=True)
                dest = dest_state_dir / src.name
                
                shutil.copy2(src, dest)
                cleaned_count += 1
            
            self.stats["final_cleaned_files"] = cleaned_count
            self.stats["duplicate_files_removed"] = (
                dedup_report["removed_count"]
            )
            
            return
        
        # Option 2: Old simple deduplication (generator reference matching)
        # Build set of files to keep based on deduplication decisions
        if deduplicate and self.permit_versions:
            for version_group in self.permit_versions:
                versioned_permits.add(version_group["permit_number"])
                if version_group.get("files_to_keep"):
                    files_to_keep.update(version_group["files_to_keep"])
                    
                    # Track deduplication stats
                    if version_group.get("is_true_duplicate"):
                        self.stats["true_duplicates_found"] += 1
                        removed_count = (
                            version_group["version_count"]
                            - len(version_group["files_to_keep"])
                        )
                        self.stats["duplicate_files_removed"] += removed_count
        
        for permit_number, files in permit_groups.items():
            for file_info in files:
                # Skip files with zero generators
                if file_info["generator_count"] == 0:
                    continue
                
                # If deduplicating, check if this file should be kept
                if deduplicate and permit_number in versioned_permits:
                    # This permit has multiple versions - use files_to_keep list
                    if file_info["path"].name not in files_to_keep:
                        continue  # Skip this duplicate
                # If permit not in versioned_permits, keep it (no version conflict)
                
                src = file_info["path"]
                dest_state_dir = self.cleaned_dir / file_info["state"]
                dest_state_dir.mkdir(exist_ok=True)
                dest = dest_state_dir / src.name
                
                shutil.copy2(src, dest)
                cleaned_count += 1
        
        self.stats["final_cleaned_files"] = cleaned_count
        self.stats["files_with_generators"] = sum(
            1 for files in permit_groups.values()
            for f in files if f["generator_count"] > 0
        )
    
    def generate_reports(self):
        """Generate detailed reports on consolidation process."""
        timestamp = datetime.now().isoformat()
        
        # Summary report
        summary_report = {
            "consolidation_date": timestamp,
            "input_directory": str(self.extracted_dir),
            "output_directory": str(self.output_dir),
            "deduplication_strategy": self.dedup_strategy,
            "statistics": self.stats,
            "zero_generator_files_count": len(self.zero_gen_files),
            "permit_version_groups_count": len(self.permit_versions),
            "duplicate_groups_count": len(self.duplicate_groups),
            "errors_count": len(self.errors)
        }
        
        with open(
            self.reports_dir / "consolidation_summary.json", "w", encoding="utf-8"
        ) as f:
            json.dump(summary_report, f, indent=2)
        
        # Zero generator files report
        zero_gen_report = {
            "total_count": len(self.zero_gen_files),
            "files": [
                {
                    "filename": f["path"].name,
                    "state": f["state"],
                    "permit_number": f["permit_number"],
                    "source_file": f["source_file"],
                    "completeness": f["completeness"]
                }
                for f in self.zero_gen_files
            ]
        }
        
        with open(
            self.reports_dir / "zero_generator_files.json", "w", encoding="utf-8"
        ) as f:
            json.dump(zero_gen_report, f, indent=2)
        
        # Permit versions report
        versions_report = {
            "total_permit_version_groups": len(self.permit_versions),
            "description": "Permits with multiple files - may represent different versions, amendments, or extraction attempts. All versions are preserved in cleaned dataset.",
            "version_groups": self.permit_versions
        }
        
        with open(
            self.reports_dir / "permit_versions.json", "w", encoding="utf-8"
        ) as f:
            json.dump(versions_report, f, indent=2)
        
        # Duplicate groups report (for smart deduplication)
        if self.duplicate_groups:
            duplicates_report = {
                "total_duplicates_removed": len(self.duplicate_groups),
                "deduplication_strategy": self.dedup_strategy,
                "description": f"Facilities identified as duplicates using {self.dedup_strategy} strategy",
                "duplicate_groups": self.duplicate_groups
            }
            
            with open(
                self.reports_dir / "duplicates_removed.json", "w", encoding="utf-8"
            ) as f:
                json.dump(duplicates_report, f, indent=2)
        
        # Errors report (if any)
        if self.errors:
            with open(
                self.reports_dir / "errors.json", "w", encoding="utf-8"
            ) as f:
                json.dump({"errors": self.errors}, f, indent=2)
    
    def clean(self, deduplicate: bool = False, smart_dedup: bool = False) -> dict:
        """Run the full cleaning process and return stats."""
        # Collect all files
        permit_groups = self.collect_all_files()
        
        # Identify permit versions (with optional simple deduplication)
        if not smart_dedup:
            self.permit_versions = self.identify_permit_versions(
                permit_groups, deduplicate=deduplicate
            )
        
        # Setup directories
        self.setup_directories()
        
        # Create cleaned dataset (respecting deduplication if enabled)
        self.create_cleaned_dataset(
            permit_groups, 
            deduplicate=deduplicate,
            smart_dedup=smart_dedup
        )
        
        # Generate reports
        self.generate_reports()
        
        return self.stats
