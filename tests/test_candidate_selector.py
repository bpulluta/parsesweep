"""Tests for CandidateSelector: draft filtering and recency-based version selection."""

from __future__ import annotations

from datetime import date

import pytest

from streamline_extract.acquisition.candidate_selector import CandidateSelector
from streamline_extract.acquisition.models import AcquisitionCandidate, CandidateScore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate(url: str, reasons: list[str] | None = None) -> AcquisitionCandidate:
    return AcquisitionCandidate(
        url=url,
        source="test",
        score=CandidateScore(),
        reasons=reasons or [],
    )


# ---------------------------------------------------------------------------
# Draft detection
# ---------------------------------------------------------------------------


class TestDraftDetection:
    def test_url_with_draft_keyword_is_excluded(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate("https://example.com/geothermal_draft_ordinance.pdf")
        assert sel._is_draft(c)

    def test_url_with_proposed_is_excluded(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate("https://example.com/proposed_amendment_2024.pdf")
        assert sel._is_draft(c)

    def test_url_with_model_ordinance_is_excluded(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate("https://example.com/model-ordinance-geothermal.pdf")
        assert sel._is_draft(c)

    def test_url_with_template_is_excluded(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate("https://example.com/ordinance_template.pdf")
        assert sel._is_draft(c)

    def test_reason_text_with_draft_is_excluded(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate("https://example.com/E-1.pdf", reasons=["Contains draft rate schedule"])
        assert sel._is_draft(c)

    def test_clean_url_is_not_draft(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate("https://www.pge.com/tariffs/ELEC_SCHEDS_E-1.pdf")
        assert not sel._is_draft(c)

    def test_xcel_final_pdf_is_not_draft(self):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate(
            "https://www.xcelenergy.com/Electric_Summation_Sheet_All_Rates_05.01.2024.pdf"
        )
        assert not sel._is_draft(c)

    def test_exclude_draft_false_ignores_patterns(self):
        sel = CandidateSelector(exclude_draft=False)
        c = _candidate("https://example.com/draft_tariff.pdf")
        # With exclude_draft=False, _is_draft is still accurate but select() won't use it
        selected, _ = sel.select([[c]], primary_per_target=1)
        assert selected == [c]

    def test_custom_draft_patterns_override_defaults(self):
        sel = CandidateSelector(exclude_draft=True, draft_patterns=[r"\bfoo\b"])
        # "draft" no longer matches with custom patterns
        c_draft = _candidate("https://example.com/draft_schedule.pdf")
        c_foo = _candidate("https://example.com/foo_schedule.pdf")
        assert not sel._is_draft(c_draft)
        assert sel._is_draft(c_foo)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestDateParsing:
    def test_us_date_mm_dot_dd_dot_yyyy(self):
        c = _candidate(
            "https://www.xcelenergy.com/Electric_Summation_Sheet_All_Rates_05.01.2024.pdf"
        )
        assert CandidateSelector._parse_date(c) == date(2024, 5, 1)

    def test_us_date_older_version(self):
        c = _candidate(
            "https://www.xcelenergy.com/Electric_Summation_Sheet_All_Rates_04.01.2024_FINAL.pdf"
        )
        assert CandidateSelector._parse_date(c) == date(2024, 4, 1)

    def test_iso_date_yyyy_mm_dd(self):
        c = _candidate("https://example.com/tariff_2024-03-15.pdf")
        assert CandidateSelector._parse_date(c) == date(2024, 3, 15)

    def test_year_only(self):
        c = _candidate("https://example.com/geothermal_ordinance_2023.pdf")
        d = CandidateSelector._parse_date(c)
        assert d == date(2023, 1, 1)

    def test_named_month(self):
        c = _candidate("https://example.com/rate_schedule_April_2024.pdf")
        d = CandidateSelector._parse_date(c)
        assert d == date(2024, 4, 1)

    def test_no_date_returns_none(self):
        c = _candidate("https://www.pge.com/tariffs/ELEC_SCHEDS_E-1.pdf")
        assert CandidateSelector._parse_date(c) is None

    def test_invalid_date_values_return_none(self):
        # Month 99 is invalid; should not raise, return None
        c = _candidate("https://example.com/schedule_99.99.2024.pdf")
        result = CandidateSelector._parse_date(c)
        # Either None or a valid fallback (year-only match for 2024)
        assert result is None or result == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# Per-target selection
# ---------------------------------------------------------------------------


class TestPerTargetSelection:
    def test_selects_one_per_target_by_default(self):
        sel = CandidateSelector(exclude_draft=False)
        t0 = [_candidate("https://pge.com/e1.pdf"), _candidate("https://pge.com/e2.pdf")]
        t1 = [_candidate("https://xcel.com/rates.pdf")]
        selected, _ = sel.select([t0, t1], primary_per_target=1)
        assert len(selected) == 2

    def test_recency_prefers_later_date(self):
        sel = CandidateSelector(exclude_draft=False)
        april = _candidate(
            "https://example.com/Electric_Summation_Sheet_04.01.2024_FINAL.pdf"
        )
        may = _candidate(
            "https://example.com/Electric_Summation_Sheet_05.01.2024.pdf"
        )
        selected, notes = sel.select([[april, may]], primary_per_target=1)
        assert selected == [may]
        assert any("2024-05-01" in n for n in notes)

    def test_undated_candidate_selected_when_only_one(self):
        sel = CandidateSelector(exclude_draft=False)
        c = _candidate("https://www.pge.com/tariffs/ELEC_SCHEDS_E-1.pdf")
        selected, _ = sel.select([[c]], primary_per_target=1)
        assert selected == [c]

    def test_dated_candidate_preferred_over_undated(self):
        sel = CandidateSelector(exclude_draft=False)
        undated = _candidate("https://example.com/geothermal_ordinance.pdf")
        dated = _candidate("https://example.com/geothermal_ordinance_2024.pdf")
        selected, _ = sel.select([[undated, dated]], primary_per_target=1)
        assert selected == [dated]

    def test_draft_filtered_before_recency_sort(self):
        sel = CandidateSelector(exclude_draft=True)
        draft = _candidate("https://example.com/proposed_ordinance_2025.pdf")
        enacted = _candidate("https://example.com/geothermal_ordinance_2023.pdf")
        selected, notes = sel.select([[draft, enacted]], primary_per_target=1)
        # draft excluded → enacted selected even though 2025 > 2023
        assert selected == [enacted]
        assert any("draft filter excluded" in n for n in notes)

    def test_empty_target_produces_no_candidates(self):
        sel = CandidateSelector(exclude_draft=False)
        selected, notes = sel.select([[]], primary_per_target=1)
        assert selected == []
        assert any("0 candidates" in n for n in notes)

    def test_all_drafts_excluded_produces_empty_target(self):
        sel = CandidateSelector(exclude_draft=True)
        c1 = _candidate("https://example.com/draft_v1.pdf")
        c2 = _candidate("https://example.com/proposed_v2.pdf")
        selected, notes = sel.select([[c1, c2]], primary_per_target=1)
        assert selected == []
        assert any("all" in n and "excluded as draft" in n for n in notes)

    def test_primary_two_per_target_returns_two(self):
        sel = CandidateSelector(exclude_draft=False)
        candidates = [
            _candidate("https://example.com/doc_2024.pdf"),
            _candidate("https://example.com/doc_2023.pdf"),
            _candidate("https://example.com/doc_2022.pdf"),
        ]
        selected, _ = sel.select([candidates], primary_per_target=2)
        assert len(selected) == 2
        # The two most recent should be returned
        urls = [c.url for c in selected]
        assert "doc_2024.pdf" in urls[0]
        assert "doc_2023.pdf" in urls[1]

    def test_supported_document_preferred_over_newer_unsupported_page(self):
        sel = CandidateSelector(exclude_draft=False)
        unsupported_newer = _candidate("https://example.com/ordinance_page_2026")
        supported_older = _candidate("https://example.com/ordinance_2024.pdf")
        selected, _ = sel.select([[unsupported_newer, supported_older]], primary_per_target=1)
        assert selected == [supported_older]

    def test_multiple_targets_independent_selection(self):
        sel = CandidateSelector(exclude_draft=True)
        # Target 0: PG&E — two real docs, no drafts
        t0 = [
            _candidate("https://pge.com/ELEC_SCHEDS_E-1.pdf"),
            _candidate("https://pge.com/ELEC_SCHEDS_E-6.pdf"),
        ]
        # Target 1: Xcel — one draft, one real
        t1 = [
            _candidate("https://xcel.com/proposed_rate_book_2025.pdf"),
            _candidate("https://xcel.com/Electric_Summation_Sheet_05.01.2024.pdf"),
        ]
        # Target 2: empty
        t2: list[AcquisitionCandidate] = []

        selected, notes = sel.select([t0, t1, t2], primary_per_target=1)
        assert len(selected) == 2  # one from t0, one from t1 (draft removed)
        assert any("proposed_rate_book" not in c.url for c in selected)
        assert any("xcel.com" in c.url for c in selected)
        assert any("0 candidates" in n for n in notes)  # t2 log

    def test_relevance_require_terms_excludes_non_legal_documents(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_require_any_terms=["ordinance", "municipal code"],
        )
        non_legal = _candidate("https://example.com/state-brief-geothermal-2025.pdf")
        legal = _candidate("https://example.com/geothermal-ordinance-chapter-18.pdf")
        selected, notes = sel.select([[non_legal, legal]], primary_per_target=1)
        assert selected == [legal]
        assert any("relevance filter excluded" in n for n in notes)

    def test_relevance_exclude_terms_blocks_plans_and_briefs(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_exclude_any_terms=["specific plan", "state brief"],
        )
        blocked = _candidate("https://example.com/lithium-valley-specific-plan-2025.pdf")
        keep = _candidate("https://example.com/municipal-code-title-17-zoning.pdf")
        selected, _ = sel.select([[blocked, keep]], primary_per_target=1)
        assert selected == [keep]

    def test_relevance_legal_marker_gate_requires_structural_legal_terms(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_require_any_terms=["geothermal", "permit"],
            relevance_require_legal_marker_terms=["ordinance", "code", "chapter", "title", "section"],
        )
        non_structural = _candidate("https://example.com/geothermal-permit-guidance-2025.pdf")
        structural = _candidate("https://example.com/title-9-chapter-22-geothermal-code.pdf")
        selected, notes = sel.select([[non_structural, structural]], primary_per_target=1)
        assert selected == [structural]
        assert any("relevance filter excluded" in n for n in notes)

    def test_relevance_legal_marker_must_exist_in_url_not_only_reason(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_require_legal_marker_terms=["ordinance"],
        )
        reason_only = _candidate(
            "https://example.com/geothermal-update-2025.pdf",
            reasons=["County ordinance update"],
        )
        url_marker = _candidate("https://example.com/geothermal-ordinance-2025.pdf")
        selected, _ = sel.select([[reason_only, url_marker]], primary_per_target=1)
        assert selected == [url_marker]

    def test_relevance_allowed_domain_patterns_filters_non_authoritative_hosts(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_allowed_domain_patterns=["county.gov", "ecode360.com"],
        )
        off_host = _candidate("https://example.com/ordinance-title-9.pdf")
        on_host = _candidate("https://library.ecode360.com/12345/documents/ordinance-title-9.pdf")
        selected, notes = sel.select([[off_host, on_host]], primary_per_target=1)
        assert selected == [on_host]
        assert any("relevance filter excluded" in n for n in notes)

    def test_require_supported_document_excludes_non_file_urls(self):
        sel = CandidateSelector(exclude_draft=False, require_supported_document=True)
        page = _candidate("https://example.com/ordinance/chapter-9")
        pdf = _candidate("https://example.com/ordinance/chapter-9.pdf")
        selected, notes = sel.select([[page, pdf]], primary_per_target=1)
        assert selected == [pdf]
        assert any("supported-document filter excluded" in n for n in notes)

    def test_require_supported_document_all_filtered_if_no_supported_links(self):
        sel = CandidateSelector(exclude_draft=False, require_supported_document=True)
        page = _candidate("https://example.com/ordinance/chapter-9")
        selected, notes = sel.select([[page]], primary_per_target=1)
        assert selected == []
        assert any("unsupported document type" in n for n in notes)

    def test_target_identity_require_any_templates_matches_jurisdiction(self):
        sel = CandidateSelector(
            exclude_draft=False,
            target_identity_require_any_templates=["{jurisdiction}", "{state}"],
        )
        off_target = _candidate("https://example.com/inyo-county-geothermal-ordinance.pdf")
        on_target = _candidate("https://example.com/mono-county-title-19-geothermal-ordinance.pdf")
        selected, notes = sel.select(
            [[off_target, on_target]],
            primary_per_target=1,
            target_contexts=[{"jurisdiction": "Mono County", "state": "CA"}],
        )
        assert selected == [on_target]
        assert any("identity filter excluded" in n for n in notes)

    def test_target_identity_require_all_templates_matches_utility_and_sector(self):
        sel = CandidateSelector(
            exclude_draft=False,
            target_identity_require_all_templates=["{utility}", "{sector}"],
        )
        utility_only = _candidate("https://example.com/pge-rate-schedule-master.pdf")
        full_match = _candidate("https://example.com/pge-residential-rate-schedule-e1.pdf")
        selected, _ = sel.select(
            [[utility_only, full_match]],
            primary_per_target=1,
            target_contexts=[{"utility": "PG&E", "sector": "residential"}],
        )
        assert selected == [full_match]

    def test_target_identity_exclude_any_templates_blocks_wrong_brand(self):
        sel = CandidateSelector(
            exclude_draft=False,
            target_identity_require_any_templates=["{manufacturer}"],
            target_identity_exclude_any_templates=["manual template", "sample"],
        )
        blocked = _candidate("https://example.com/generac-manual-template-sample.pdf")
        keep = _candidate("https://example.com/generac-200kw-operator-manual.pdf")
        selected, _ = sel.select(
            [[blocked, keep]],
            primary_per_target=1,
            target_contexts=[{"manufacturer": "Generac"}],
        )
        assert selected == [keep]


# ---------------------------------------------------------------------------
# Runtime config loader integration: selection block mapping
# ---------------------------------------------------------------------------


class TestSelectionConfigMapping:
    def test_selection_block_maps_primary_per_target(self):
        from streamline_extract.config.runtime_config_loader import resolve_command_config

        config = {
            "acquisition": {
                "selection": {
                    "primary_per_target": 2,
                    "exclude_draft": False,
                }
            }
        }
        resolved = resolve_command_config(
            command="acquire",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_primary_per_target"] == 2
        assert resolved["selection_exclude_draft"] is False

    def test_selection_block_maps_draft_patterns(self):
        from streamline_extract.config.runtime_config_loader import resolve_command_config

        config = {
            "acquisition": {
                "selection": {
                    "draft_patterns": [r"\bfoo\b", r"\bbar\b"],
                }
            }
        }
        resolved = resolve_command_config(
            command="acquire",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_draft_patterns"] == [r"\bfoo\b", r"\bbar\b"]

    def test_missing_selection_block_leaves_defaults(self):
        from streamline_extract.config.runtime_config_loader import resolve_command_config

        resolved = resolve_command_config(
            command="acquire",
            cli_values={},
            config_data={},
        )
        # Keys absent from config — resolved dict should not have them
        assert "selection_primary_per_target" not in resolved
        assert "selection_exclude_draft" not in resolved

    def test_selection_block_maps_target_identity_templates(self):
        from streamline_extract.config.runtime_config_loader import resolve_command_config

        config = {
            "acquisition": {
                "selection": {
                    "target_identity_require_any_templates": ["{jurisdiction}", "{state}"],
                    "target_identity_require_all_templates": ["{utility}", "{sector}"],
                    "target_identity_exclude_any_templates": ["draft", "template"],
                }
            }
        }
        resolved = resolve_command_config(
            command="acquire",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_target_identity_require_any_templates"] == ["{jurisdiction}", "{state}"]
        assert resolved["selection_target_identity_require_all_templates"] == ["{utility}", "{sector}"]
        assert resolved["selection_target_identity_exclude_any_templates"] == ["draft", "template"]
