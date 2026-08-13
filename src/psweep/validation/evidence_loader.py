"""
Evidence loader for QA/QC reports.

Loads source documents, extraction JSON outputs, and discovery metadata
to populate evidence columns in comparison reports.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class EvidenceLoader:
    """Load and cache evidence metadata and extracted values for report generation."""

    def __init__(self, schema: Optional[Dict[str, Any]] = None):
        self._doc_metadata_cache: Dict[str, Dict[str, Any]] = {}
        self._model_outputs_cache: Dict[str, Dict[str, Any]] = {}
        # Path-keyed guards so a source file is parsed once even when the loader
        # is reused across many documents in a single run (the discovery
        # checkpoint in particular is shared by every document).
        self._metadata_source: Optional[Path] = None
        self._extraction_source: Optional[Path] = None
        self._schema = schema or {}
        self._main_data_array = self._extract_main_data_array()

    def load_document_metadata(self, discovery_checkpoint_path: Path) -> Dict[str, Dict[str, Any]]:
        """
        Load document metadata from discovery checkpoint.

        Cached by checkpoint path: the discovery checkpoint is shared by every
        document in a run, so it is read and parsed only once even when this
        loader is reused across many report generations.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            {doc_id: {file_path, sections, page_count, ...}}

        Raises
        ------
        FileNotFoundError
            If checkpoint path is provided but doesn't exist.
        json.JSONDecodeError
            If checkpoint JSON is malformed.
        """
        if (
            self._metadata_source == discovery_checkpoint_path
            and self._doc_metadata_cache
        ):
            return self._doc_metadata_cache

        if not discovery_checkpoint_path.exists():
            raise FileNotFoundError(
                f"Discovery checkpoint missing (required for evidence display): {discovery_checkpoint_path}. "
                f"Run discovery first or provide a valid checkpoint path."
            )

        try:
            with open(discovery_checkpoint_path) as f:
                checkpoint = json.load(f)

            metadata = {}
            entries = checkpoint.get("entries", {})
            for doc_id, entry in entries.items():
                if isinstance(entry, dict):
                    metadata[doc_id] = {
                        "file_path": entry.get("file_path", ""),
                        "sections": entry.get("sections", []),
                        "page_count": entry.get("page_count"),
                        "mime_type": entry.get("mime_type", ""),
                    }
            self._doc_metadata_cache = metadata
            self._metadata_source = discovery_checkpoint_path
            return metadata
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Failed to parse discovery checkpoint JSON (may be corrupt): {discovery_checkpoint_path}",
                e.doc,
                e.pos,
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load discovery checkpoint from {discovery_checkpoint_path}: {e}"
            ) from e

    def load_extraction_outputs(self, extraction_dir: Path, models: list) -> Dict[str, Dict[str, Any]]:
        """
        Load extraction JSON outputs for all models in a directory.

        Parameters
        ----------
        extraction_dir : Path
            Path to validation/<doc_id>/ directory
        models : list
            List of model names (e.g., ['gpt-5.6-terra', 'claude-sonnet-4-6'])

        Returns
        -------
        Dict[str, Dict[str, Any]]
            {model_name: {extracted JSON structure}}

        Raises
        ------
        RuntimeError
            If required model extraction files are missing or corrupt.
        """
        outputs = {}
        missing_models = []
        failed_models = {}

        if (
            self._extraction_source == extraction_dir
            and self._model_outputs_cache
        ):
            return self._model_outputs_cache

        for model in models:
            json_path = extraction_dir / f"{model}.json"
            if json_path.exists():
                try:
                    with open(json_path) as f:
                        outputs[model] = json.load(f)
                except json.JSONDecodeError as e:
                    failed_models[model] = f"Corrupt JSON: {e}"
                except Exception as e:
                    failed_models[model] = str(e)
            else:
                missing_models.append(model)
        
        # Fail loudly if any critical extractions are missing or corrupt
        if missing_models or failed_models:
            error_parts = []
            if missing_models:
                error_parts.append(f"Missing extraction files: {', '.join(missing_models)}")
            if failed_models:
                error_details = "; ".join(f"{m}: {e}" for m, e in failed_models.items())
                error_parts.append(f"Corrupt extraction files: {error_details}")
            raise RuntimeError(
                f"Cannot load extraction outputs from {extraction_dir}: {' | '.join(error_parts)}"
            )
        
        self._model_outputs_cache = outputs
        self._extraction_source = extraction_dir
        return outputs

    def _extract_main_data_array(self) -> Optional[str]:
        """Extract main_data_array name from schema $metadata.

        Raises
        ------
        ValueError
            If schema is provided but missing required $metadata structure.
        """
        if not self._schema:
            logger.warning("No schema provided to EvidenceLoader; evidence columns will be empty")
            return None
            
        try:
            metadata = self._schema.get("$metadata", {})
            if not metadata:
                raise ValueError(
                    "Schema is missing required '$metadata' section. "
                    "All v2.0+ schemas must declare $metadata.extraction.main_data_array"
                )
            extraction = metadata.get("extraction", {})
            if not extraction:
                raise ValueError(
                    "Schema $metadata is missing required 'extraction' block. "
                    "Must declare: $metadata.extraction.main_data_array"
                )
            main_array = extraction.get("main_data_array")
            if not main_array:
                raise ValueError(
                    "Schema $metadata.extraction is missing required 'main_data_array' field. "
                    "This field must declare which array key contains the extracted records."
                )
            return main_array
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Failed to extract main_data_array from schema: {e}. "
                f"Schema structure may be corrupted."
            ) from e

    def _get_main_data_array(self, output: Dict[str, Any]) -> Optional[list]:
        """Navigate to main data array in extraction output.
        
        Schema MUST declare the correct location via $metadata.extraction.main_data_array.
        No heuristics or fallbacks - if the declared location doesn't exist, fail loudly.
        """
        if not self._main_data_array:
            # This should have been caught in __init__, but double-check
            raise ValueError(
                "Schema did not declare main_data_array. Cannot locate extracted records. "
                "Ensure schema has $metadata.extraction.main_data_array field."
            )
        
        # Check top-level first (most common case)
        if self._main_data_array in output and isinstance(output[self._main_data_array], list):
            return output[self._main_data_array]
        
        # Check inside 'payload' wrapper (for specific schemas like data center timelines)
        # This is NOT a fallback heuristic - if schema declares array is at payload.X,
        # we must look there. If it's not there either, the extraction is broken.
        if "payload" in output and isinstance(output["payload"], dict):
            if self._main_data_array in output["payload"] and isinstance(output["payload"][self._main_data_array], list):
                return output["payload"][self._main_data_array]
        
        # Array not found at any expected location - fail loudly
        raise ValueError(
            f"Extraction output does not contain main_data_array '{self._main_data_array}'. "
            f"Expected either at top-level or in payload.{self._main_data_array}. "
            f"Schema $metadata.extraction.main_data_array may be incorrect or extraction may be corrupted. "
            f"Available keys: {', '.join(output.keys())}"
        )

    def get_document_path(self, doc_id: str) -> Optional[str]:
        """Get file path for a document. Returns None if metadata not loaded or doc not found."""
        if not self._doc_metadata_cache:
            return None
        entry = self._doc_metadata_cache.get(doc_id)
        if not entry:
            logger.warning(f"Document '{doc_id}' not found in metadata cache")
            return None
        return entry.get("file_path")

    def get_section_for_item(self, doc_id: str, item_path: str) -> Optional[str]:
        """
        Get section/heading for an extracted item.

        Parameters
        ----------
        doc_id : str
            Document identifier
        item_path : str
            Item path in extraction JSON (e.g., "facilities › requirements › structures_distance")

        Returns
        -------
        Optional[str]
            Section name if available, else None
        """
        if not self._doc_metadata_cache:
            return None
        entry = self._doc_metadata_cache.get(doc_id)
        if not entry:
            logger.warning(f"Document '{doc_id}' not found in metadata cache")
            return None
        
        sections = entry.get("sections", [])
        # Simple heuristic: find the most relevant section based on item path
        if sections and isinstance(sections, list):
            return sections[0] if sections else None
        return None

    def extract_model_values(self, model_name: str, item_id: str) -> Dict[str, Any]:
        """
        Extract structured values (value, obligation, units) from model output.

        Parameters
        ----------
        model_name : str
            Name of the model
        item_id : str
            Item identifier (e.g., "facilities|requirements|structures_distance")

        Returns
        -------
        Dict[str, Any]
            {value: ..., obligation: ..., units: ...} or empty dict if not found
        """
        if model_name not in self._model_outputs_cache:
            logger.warning(f"Extraction output for model '{model_name}' not found in cache")
            return {}

        output = self._model_outputs_cache[model_name]
        extracted = {}

        # Navigate to the main data array
        try:
            array = self._get_main_data_array(output)
            if array:
                for item in array:
                    if self._matches_item_id(item, item_id):
                        extracted = self._collect_item_values(item)
                        break
        except ValueError:
            # Schema or extraction structure issue - log and continue
            pass
        
        return extracted

    def _matches_item_id(self, item: Dict[str, Any], item_id: str) -> bool:
        """Check if an extracted item matches the given item_id."""
        # Simple check: look for matching key fields
        if "id" in item and str(item["id"]) == item_id:
            return True
        # Add more matching logic as needed
        return False

    def _collect_item_values(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Collect value, obligation, units from an extracted item."""
        result = {}
        if "value" in item:
            result["value"] = item["value"]
        if "obligation" in item:
            result["obligation"] = item["obligation"]
        if "units" in item:
            result["units"] = item["units"]
        return result

    def format_model_evidence(
        self,
        model_name: str,
        item_id: str,
        truncate: int = 200,
    ) -> str:
        """
        Format extracted model values as readable string.

        Parameters
        ----------
        model_name : str
            Name of the model
        item_id : str
            Item identifier
        truncate : int
            Maximum chars for any single value

        Returns
        -------
        str
            Formatted string like "value=1 mile; obligation=required; units=distance"
            or "(not extracted)" if not found
        """
        values = self.extract_model_values(model_name, item_id)
        if not values:
            return "(not extracted)"

        parts = []
        if "value" in values:
            val_str = str(values["value"])[:truncate]
            parts.append(f"value={val_str}")
        if "obligation" in values:
            parts.append(f"obligation={values['obligation']}")
        if "units" in values:
            parts.append(f"units={values['units']}")

        return "; ".join(parts) if parts else "(not extracted)"
