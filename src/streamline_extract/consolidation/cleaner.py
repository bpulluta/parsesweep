"""Data cleaning module for document extraction - removes empty files and identifies versions."""

import json
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional
import logging

from ..utils.exceptions import SchemaMetadataError

logger = logging.getLogger(__name__)


class ExtractionCleaner:
    """Cleans and organizes extracted document data."""
    
    def __init__(self, extracted_dir: Path, output_dir: Path, schema_metadata):
        """
        Initialize cleaner.
        
        Args:
            extracted_dir: Directory containing extraction files
            output_dir: Output directory for cleaned files
            schema_metadata: SchemaMetadata instance (required in v2.0+)
            
        Raises:
            SchemaMetadataError: If schema_metadata is not provided
        """
        if not schema_metadata:
            raise SchemaMetadataError(
                "ExtractionCleaner requires schema metadata.\n"
                "Schema metadata is required as of StreamlineExtract v2.0."
            )
        
        self.extracted_dir = Path(extracted_dir)
        self.output_dir = Path(output_dir)
        self.schema_metadata = schema_metadata
        
        # Output subdirectories (no raw copy - source is already raw)
        self.cleaned_dir = self.output_dir
        self.reports_dir = self.output_dir / "reports"
        
        # Stats tracking
        self.stats = {
            "total_files": 0,
            "zero_item_files": 0,
            "entity_version_groups": 0,
            "files_with_items": 0,
            "final_cleaned_files": 0,
            "true_duplicates_found": 0,
            "duplicate_files_removed": 0,
        }
        
        # Tracking for reports
        self.zero_item_files = []
        self.entity_versions = []
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
        Collect all extraction files grouped by entity identifier.
        Returns: {entity_id: [file_info_dicts]}
        """
        entity_groups = defaultdict(list)
        
        for category_dir in self.extracted_dir.iterdir():
            if not category_dir.is_dir():
                continue
                
            for json_file in category_dir.glob("*.json"):
                data = self.load_extraction(json_file)
                if data is None:
                    continue
                
                self.stats["total_files"] += 1
                
                # Get entity identifier using schema metadata (required)
                entity_id = self.schema_metadata.extract_identifier_from_data(data)
                
                # Detect item count using metadata
                item_count = self._detect_item_count(data)
                
                file_info = {
                    "path": json_file,
                    "relative_path": json_file.relative_to(self.extracted_dir),
                    "category": category_dir.name,
                    "entity_id": entity_id,
                    "item_count": item_count,
                    "completeness": data.get("completeness_score", 0.0),
                    "source_file": data.get("source_file", ""),
                    "data": data,
                }
                
                # Track zero-item files
                if item_count == 0:
                    self.zero_item_files.append(file_info)
                    self.stats["zero_item_files"] += 1
                
                entity_groups[entity_id].append(file_info)
        
        return entity_groups
    
    def identify_entity_versions(
        self, entity_groups: dict[str, list[dict]], deduplicate: bool = False
    ) -> list[dict]:
        """
        Identify entities with multiple versions.
        We preserve all versions since they may contain different items.
        
        Args:
            entity_groups: Dictionary of entity_id -> list of file_info dicts
            deduplicate: If True, detect and mark true duplicates for removal
        """
        version_reports = []
        
        for entity_id, files in entity_groups.items():
            if len(files) > 1:
                self.stats["entity_version_groups"] += 1
                
                # Sort by completeness, then by item count
                sorted_files = sorted(
                    files,
                    key=lambda x: (
                        x["completeness"],
                        x["item_count"]
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
                    "entity_id": entity_id,
                    "version_count": len(files),
                    "category": files[0]["category"],
                    "is_true_duplicate": is_duplicate,
                    "duplicate_reason": duplicate_reason,
                    "files_to_keep": [f["path"].name for f in files_to_keep] if deduplicate else None,
                    "versions": [
                        {
                            "file": f["path"].name,
                            "source_pdf": f["source_file"],
                            "items": f["item_count"],
                            "completeness": f["completeness"],
                            "cost_usd": f["data"].get("cost_usd", 0),
                            "kept": f in files_to_keep if deduplicate else True
                        }
                        for f in sorted_files
                    ],
                    "total_items_across_versions": sum(
                        f["item_count"] for f in files
                    ),
                    "recommendation": "All versions preserved - may represent amendments or different extractions"
                })
        
        return version_reports
    
    def _check_true_duplicate(
        self, files: list[dict]
    ) -> tuple[bool, str | None, list[dict]]:
        """
        Check if multiple files are true duplicates (same items).
        
        Returns:
            (is_duplicate, reason, files_to_keep)
        """
        if len(files) < 2:
            return False, None, files
        
        # Get item identifiers from each file (works with any array field)
        item_id_sets = []
        for file_info in files:
            data_obj = file_info["data"].get("data", {})
            
            # Find main array field (generatorSets, requirements, rates, etc.)
            item_ids = set()
            for key, value in data_obj.items():
                if isinstance(value, list) and value:
                    # Extract identifiers from items
                    for item in value:
                        if isinstance(item, dict):
                            # Try common identifier fields
                            for id_field in ["referenceNumber", "id", "feature", "name", "description"]:
                                if id_field in item and item[id_field]:
                                    item_ids.add(str(item[id_field]))
                                    break
            item_id_sets.append(item_ids)
        
        # Check if all files have the same item identifiers
        if len(item_id_sets) < 2 or not item_id_sets[0]:
            return False, None, files
        
        first_set = item_id_sets[0]
        all_same = all(ids == first_set for ids in item_id_sets[1:])
        
        if all_same and len(first_set) > 0:
            # True duplicate - keep only the best (first in sorted list)
            reason = f"All {len(files)} versions have identical {len(first_set)} item identifiers"
            return True, reason, [files[0]]
        
        # Different items - keep all
        return False, None, files
    
    def create_cleaned_dataset(
        self, entity_groups: dict, deduplicate: bool = False
    ):
        """Create cleaned dataset with ALL files that have items."""
        cleaned_count = 0
        files_to_keep = set()
        versioned_entities = set()
        
        # Build set of files to keep based on deduplication decisions
        if deduplicate and self.entity_versions:
            for version_group in self.entity_versions:
                versioned_entities.add(version_group["entity_id"])
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
        
        for entity_id, files in entity_groups.items():
            for file_info in files:
                # Skip files with zero items
                if file_info["item_count"] == 0:
                    continue
                
                # If deduplicating, check if this file should be kept
                if deduplicate and entity_id in versioned_entities:
                    # This entity has multiple versions - use files_to_keep list
                    if file_info["path"].name not in files_to_keep:
                        continue  # Skip this duplicate
                # If entity not in versioned_entities, keep it (no version conflict)
                
                src = file_info["path"]
                dest_category_dir = self.cleaned_dir / file_info["category"]
                dest_category_dir.mkdir(exist_ok=True)
                dest = dest_category_dir / src.name
                
                shutil.copy2(src, dest)
                cleaned_count += 1
        
        self.stats["final_cleaned_files"] = cleaned_count
        self.stats["files_with_items"] = sum(
            1 for files in entity_groups.values()
            for f in files if f["item_count"] > 0
        )
    
    def generate_reports(self):
        """Generate detailed reports on consolidation process."""
        timestamp = datetime.now().isoformat()
        
        # Summary report
        summary_report = {
            "consolidation_date": timestamp,
            "input_directory": str(self.extracted_dir),
            "output_directory": str(self.output_dir),
            "statistics": self.stats,
            "zero_item_files_count": len(self.zero_item_files),
            "entity_version_groups_count": len(self.entity_versions),
            "errors_count": len(self.errors)
        }
        
        with open(
            self.reports_dir / "consolidation_summary.json", "w", encoding="utf-8"
        ) as f:
            json.dump(summary_report, f, indent=2)
        
        # Zero-item files report
        zero_item_report = {
            "total_count": len(self.zero_item_files),
            "description": "Files with no extracted items - may represent failed extractions or empty documents",
            "files": [
                {
                    "filename": f["path"].name,
                    "category": f["category"],
                    "entity_id": f["entity_id"],
                    "source_file": f["source_file"],
                    "completeness": f["completeness"]
                }
                for f in self.zero_item_files
            ]
        }
        
        with open(
            self.reports_dir / "zero_item_files.json", "w", encoding="utf-8"
        ) as f:
            json.dump(zero_item_report, f, indent=2)
        
        # Entity versions report
        versions_report = {
            "total_entity_version_groups": len(self.entity_versions),
            "description": "Entities with multiple files - may represent different versions, amendments, or extraction attempts. All versions are preserved in cleaned dataset.",
            "version_groups": self.entity_versions
        }
        
        with open(
            self.reports_dir / "entity_versions.json", "w", encoding="utf-8"
        ) as f:
            json.dump(versions_report, f, indent=2)
        
        # Errors report (if any)
        if self.errors:
            with open(
                self.reports_dir / "errors.json", "w", encoding="utf-8"
            ) as f:
                json.dump({"errors": self.errors}, f, indent=2)
    
    def clean(self, deduplicate: bool = False) -> dict:
        """Run the full cleaning process and return stats."""
        # Collect all files
        entity_groups = self.collect_all_files()
        
        # Identify entity versions (with optional deduplication)
        self.entity_versions = self.identify_entity_versions(
            entity_groups, deduplicate=deduplicate
        )
        
        # Setup directories
        self.setup_directories()
        
        # Create cleaned dataset (respecting deduplication if enabled)
        self.create_cleaned_dataset(entity_groups, deduplicate=deduplicate)
        
        # Generate reports
        self.generate_reports()
        
        return self.stats
    
    def _detect_item_count(self, data: dict) -> int:
        """
        Detect item count from extraction data using schema metadata.
        
        Args:
            data: Extraction data dictionary
            
        Returns:
            Number of items in main data array
        """
        # Use metadata to find main array (required in v2.0+)
        main_array = self.schema_metadata.extract_main_data_array(data)
        return len(main_array)
