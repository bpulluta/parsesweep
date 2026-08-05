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
        self._schema = schema or {}
        self._main_data_array = self._extract_main_data_array()

    def load_document_metadata(self, discovery_checkpoint_path: Path) -> Dict[str, Dict[str, Any]]:
        """
        Load document metadata from discovery checkpoint.

        Returns:
            {doc_id: {file_path, sections, page_count, ...}}
        """
        if not discovery_checkpoint_path.exists():
            logger.warning(f"Discovery checkpoint not found: {discovery_checkpoint_path}")
            return {}

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
            return metadata
        except Exception as e:
            logger.error(f"Failed to load discovery checkpoint: {e}")
            return {}

    def load_extraction_outputs(self, extraction_dir: Path, models: list) -> Dict[str, Dict[str, Any]]:
        """
        Load extraction JSON outputs for all models in a directory.

        Args:
            extraction_dir: Path to qa_qc/<doc_id>/ directory
            models: List of model names (e.g., ['gpt-5.6-terra', 'claude-sonnet-4-6'])

        Returns:
            {model_name: {extracted JSON structure}}
        """
        outputs = {}
        for model in models:
            json_path = extraction_dir / f"{model}.json"
            if json_path.exists():
                try:
                    with open(json_path) as f:
                        outputs[model] = json.load(f)
                except Exception as e:
                    logger.warning(f"Failed to load extraction for {model}: {e}")
        self._model_outputs_cache = outputs
        return outputs

    def _extract_main_data_array(self) -> Optional[str]:
        """Extract main_data_array name from schema $metadata."""
        try:
            metadata = self._schema.get("$metadata", {})
            extraction = metadata.get("extraction", {})
            return extraction.get("main_data_array")
        except Exception as e:
            logger.debug(f"Could not extract main_data_array from schema: {e}")
            return None

    def _get_main_data_array(self, output: Dict[str, Any]) -> Optional[list]:
        """Navigate to main data array in extraction output (schema-driven, no fallback)."""
        if not self._main_data_array:
            logger.warning("No main_data_array declared in schema $metadata.extraction")
            return None
        
        # Check top-level first
        if self._main_data_array in output and isinstance(output[self._main_data_array], list):
            return output[self._main_data_array]
        
        # Check inside 'payload' wrapper (for schemas like data center timelines)
        if "payload" in output and isinstance(output["payload"], dict):
            if self._main_data_array in output["payload"] and isinstance(output["payload"][self._main_data_array], list):
                return output["payload"][self._main_data_array]
        
        logger.warning(
            f"Main data array '{self._main_data_array}' not found in extraction output. "
            "Check schema $metadata.extraction.main_data_array is correct."
        )
        return None

    def get_document_path(self, doc_id: str) -> Optional[str]:
        """Get file path for a document."""
        if not self._doc_metadata_cache:
            return None
        entry = self._doc_metadata_cache.get(doc_id, {})
        return entry.get("file_path")

    def get_section_for_item(self, doc_id: str, item_path: str) -> Optional[str]:
        """
        Get section/heading for an extracted item.

        Args:
            doc_id: Document identifier
            item_path: Item path in extraction JSON (e.g., "facilities › requirements › structures_distance")

        Returns:
            Section name if available, else None
        """
        if not self._doc_metadata_cache:
            return None
        entry = self._doc_metadata_cache.get(doc_id, {})
        sections = entry.get("sections", [])
        # Simple heuristic: find the most relevant section based on item path
        if sections and isinstance(sections, list):
            return sections[0] if sections else None
        return None

    def extract_model_values(self, model_name: str, item_id: str) -> Dict[str, Any]:
        """
        Extract structured values (value, obligation, units) from model output.

        Args:
            model_name: Name of the model
            item_id: Item identifier (e.g., "facilities|requirements|structures_distance")

        Returns:
            {value: ..., obligation: ..., units: ..., or empty dict if not found}
        """
        if model_name not in self._model_outputs_cache:
            return {}

        output = self._model_outputs_cache[model_name]
        extracted = {}

        # Navigate to the main data array using schema-aware or fallback heuristics
        array = self._get_main_data_array(output)
        if array:
            for item in array:
                if self._matches_item_id(item, item_id):
                    extracted = self._collect_item_values(item)
                    break
        
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

        Args:
            model_name: Name of the model
            item_id: Item identifier
            truncate: Maximum chars for any single value

        Returns:
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
