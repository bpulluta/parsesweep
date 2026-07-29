from psweep.cli import commands
from psweep.cli.commands_extract import (
    _apply_page_targeting,
    _build_index_filters,
    _build_run_manifest,
    _context_budget_suggestions_for_process,
    _extract_and_save_result,
    _extract_one_document,
    _generate_run_id,
    _resolve_schema_ref,
    _row_matches_filters,
    _run_qa_qc_extraction,
    _write_run_manifest,
    extract,
)


def test_extract_facade_reexports_canonical_symbols():
    assert commands.extract is extract
    assert commands._generate_run_id is _generate_run_id
    assert (
        commands._context_budget_suggestions_for_process
        is _context_budget_suggestions_for_process
    )
    assert commands._build_run_manifest is _build_run_manifest
    assert commands._write_run_manifest is _write_run_manifest
    assert commands._resolve_schema_ref is _resolve_schema_ref
    assert commands._build_index_filters is _build_index_filters
    assert commands._row_matches_filters is _row_matches_filters
    assert commands._extract_one_document is _extract_one_document
    assert commands._extract_and_save_result is _extract_and_save_result
    assert commands._run_qa_qc_extraction is _run_qa_qc_extraction
    assert commands._apply_page_targeting is _apply_page_targeting
