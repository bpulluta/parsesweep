"""Tests for CandidateSelector: draft filtering and recency-based version selection."""

from __future__ import annotations

from datetime import date

import pytest

from psweep.discovery.candidate_selector import CandidateSelector
from psweep.discovery.models import DiscoveryCandidate

from discovery_helpers import make_candidate as _candidate


# ---------------------------------------------------------------------------
# Draft detection
# ---------------------------------------------------------------------------


class TestDraftDetection:
    @pytest.mark.parametrize(
        ("url", "reasons", "expected"),
        [
            ("https://example.com/geothermal_draft_ordinance.pdf", None, True),
            ("https://example.com/proposed_amendment_2024.pdf", None, True),
            ("https://example.com/model-ordinance-geothermal.pdf", None, True),
            ("https://example.com/ordinance_template.pdf", None, True),
            (
                "https://example.com/ordinance-update.pdf",
                ["Contains draft rate schedule"],
                False,
            ),
            ("https://example.com/ordinance-update.pdf", ["ranked highly"], False),
            ("https://www.pge.com/tariffs/ELEC_SCHEDS_E-1.pdf", None, False),
            (
                "https://www.xcelenergy.com/Electric_Summation_Sheet_All_Rates_05.01.2024.pdf",
                None,
                False,
            ),
        ],
    )
    def test_draft_detection_patterns(self, url, reasons, expected):
        sel = CandidateSelector(exclude_draft=True)
        c = _candidate(url, reasons=reasons)
        assert sel._is_draft(c) is expected

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
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.xcelenergy.com/Electric_Summation_Sheet_All_Rates_05.01.2024.pdf",
                date(2024, 5, 1),
            ),
            (
                "https://www.xcelenergy.com/Electric_Summation_Sheet_All_Rates_04.01.2024_FINAL.pdf",
                date(2024, 4, 1),
            ),
            ("https://example.com/tariff_2024-03-15.pdf", date(2024, 3, 15)),
            ("https://example.com/geothermal_ordinance_2023.pdf", date(2023, 1, 1)),
            ("https://example.com/rate_schedule_April_2024.pdf", date(2024, 4, 1)),
            ("https://www.pge.com/tariffs/ELEC_SCHEDS_E-1.pdf", None),
        ],
    )
    def test_parse_date(self, url, expected):
        c = _candidate(url)
        assert CandidateSelector._parse_date(c) == expected

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
        t2: list[DiscoveryCandidate] = []

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

    def test_relevance_require_terms_ignores_query_text_in_reasons(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_require_any_terms=["ordinance", "compressor", "pipeline"],
        )
        reason_only = _candidate(
            "https://example.com/random-brand-landing",
            reasons=[
                "SerpApi result rank 1 for query 'Keller Texas natural gas compressor station ordinance'"
            ],
        )
        real_match = _candidate(
            "https://example.com/weatherford-compressor-ordinance.pdf"
        )
        selected, notes = sel.select([[reason_only, real_match]], primary_per_target=1)
        assert selected == [real_match]
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

    def test_relevance_exclude_terms_ignores_query_text_in_reasons(self):
        sel = CandidateSelector(
            exclude_draft=False,
            relevance_exclude_any_terms=["state brief"],
        )
        reason_only = _candidate(
            "https://example.com/real-ordinance.pdf",
            reasons=["SerpApi result rank 1 for query 'state brief ordinance'"],
        )
        selected, notes = sel.select([[reason_only]], primary_per_target=1)
        assert selected == [reason_only]
        assert not any("relevance filter excluded" in n for n in notes)

    def test_split_exclusion_patterns_can_target_url_and_text_separately(self):
        sel = CandidateSelector(
            exclude_draft=False,
            exclude_url_patterns=[r"archive"],
            exclude_text_patterns=[r"preliminary"],
        )
        url_blocked = _candidate("https://example.com/archive/ordinance-final.pdf")
        text_blocked = _candidate(
            "https://example.com/ordinance-final.pdf",
            snippet="preliminary planning packet",
        )
        keep = _candidate("https://example.com/ordinance-final.pdf")
        selected, notes = sel.select([[url_blocked, text_blocked, keep]], primary_per_target=1)
        assert selected == [keep]
        assert any("configured exclusion patterns excluded" in n for n in notes)

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

    def test_allowed_domain_no_longer_hard_filters_in_selector(self):
        # Recall-first: allowed-domain preference is now a soft ranking boost in
        # the prioritizer, not a hard reject in the selector. An off-list host
        # must NOT be dropped here.
        sel = CandidateSelector(exclude_draft=False)
        off_host = _candidate("https://example.com/ordinance-title-9.pdf")
        selected, _notes = sel.select([[off_host]], primary_per_target=1)
        assert selected == [off_host]

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

    def test_max_per_host_per_target_limits_single_host_dominance(self):
        sel = CandidateSelector(
            exclude_draft=False,
            max_per_host_per_target=1,
        )
        host_a_2025 = _candidate("https://a.example.com/site-a-2025.pdf")
        host_a_2024 = _candidate("https://a.example.com/site-a-2024.pdf")
        host_b_2023 = _candidate("https://b.example.com/site-b-2023.pdf")
        selected, notes = sel.select(
            [[host_a_2025, host_a_2024, host_b_2023]],
            primary_per_target=3,
        )
        urls = {c.url for c in selected}
        assert len(selected) == 2
        assert host_a_2025.url in urls
        assert host_b_2023.url in urls
        assert host_a_2024.url not in urls
        assert any("per-host cap=1 applied" in n for n in notes)


# ---------------------------------------------------------------------------
# Runtime config loader integration: selection block mapping
# ---------------------------------------------------------------------------


class TestSelectionConfigMapping:
    def test_selection_block_maps_primary_per_target(self):
        from psweep.config.runtime_config_loader import resolve_command_config

        config = {
            "discovery": {
                "selection": {
                    "primary_per_target": 2,
                    "exclude_draft": False,
                }
            }
        }
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_primary_per_target"] == 2
        assert resolved["selection_exclude_draft"] is False

    def test_selection_block_maps_draft_patterns(self):
        from psweep.config.runtime_config_loader import resolve_command_config

        config = {
            "discovery": {
                "selection": {
                    "draft_patterns": [r"\bfoo\b", r"\bbar\b"],
                }
            }
        }
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_draft_patterns"] == [r"\bfoo\b", r"\bbar\b"]

    def test_missing_selection_block_leaves_defaults(self):
        from psweep.config.runtime_config_loader import resolve_command_config

        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data={},
        )
        # Keys absent from config — resolved dict should not have them
        assert "selection_primary_per_target" not in resolved
        assert "selection_exclude_draft" not in resolved

    def test_selection_block_maps_target_identity_templates(self):
        from psweep.config.runtime_config_loader import resolve_command_config

        config = {
            "discovery": {
                "selection": {
                    "target_identity_require_any_templates": ["{jurisdiction}", "{state}"],
                    "target_identity_require_all_templates": ["{utility}", "{sector}"],
                    "target_identity_exclude_any_templates": ["draft", "template"],
                }
            }
        }
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_target_identity_require_any_templates"] == ["{jurisdiction}", "{state}"]
        assert resolved["selection_target_identity_require_all_templates"] == ["{utility}", "{sector}"]
        assert resolved["selection_target_identity_exclude_any_templates"] == ["draft", "template"]

    def test_selection_block_maps_max_per_host_per_target(self):
        from psweep.config.runtime_config_loader import resolve_command_config

        config = {
            "discovery": {
                "selection": {
                    "max_per_host_per_target": 2,
                }
            }
        }
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=config,
        )
        assert resolved["selection_max_per_host_per_target"] == 2
