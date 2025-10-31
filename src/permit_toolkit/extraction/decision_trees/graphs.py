"""Decision Tree setup functions"""

import networkx as nx
from elm.ords.extraction.graphs import (
    llm_response_starts_with_no,
    llm_response_starts_with_yes,
)


_SECTION_PROMPT = (
    'The value of the "section" key should be a string representing the '
    "title of the section (including numerical labels), if it's given, "
    "and `null` otherwise."
)
_COMMENT_PROMPT = (
    'The value of the "comment" key should be a one-sentence explanation '
    "of how you determined the value, if you think it is necessary "
    "(`null` otherwise)."
)


def _setup_graph_no_nodes(**kwargs):
    return nx.DiGraph(
        SECTION_PROMPT=_SECTION_PROMPT,
        COMMENT_PROMPT=_COMMENT_PROMPT,
        **kwargs,
    )


def setup_graph_permit_num(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention a permit or registration number "
            "for the application?"
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_permit_num", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_permit_num",
        prompt=(
            "What is the permit or registration number mentioned in the text?"
        ),
    )

    G.add_edge("get_permit_num", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "permit_number" and "explanation". The '
            'value of the "permit_number" key should be a string containing '
            "the permit or registration number mentioned in the text. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_permit_issue_date(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention an issue date for the permit?"
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_permit_date", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_permit_date",
        prompt="What is the stated issue date for the permit?",
    )

    G.add_edge("get_permit_date", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "issue_date" and "explanation". The '
            'value of the "issue_date" key should be a string containing '
            "the permit issue date in YYYY-MM-DD format, if unambiguous. "
            "If you could not determine a specific issue date, this key "
            "should be `null`"
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_permit_expiration_date(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text explicitly mention an expiration date "
            "for the permit? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "get_permit_expiration_date",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "get_permit_expiration_date",
        prompt="What is the given expiration date for the permit?",
    )

    G.add_edge("get_permit_expiration_date", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"permit_expiration_date" and "explanation". The '
            'value of the "permit_expiration_date" key should be a string '
            "containing the explicitly provided expiration date for the "
            "permit in YYYY-MM-DD format, if unambiguous. "
            "If the text does not explicitly give an expiration date for "
            "the permit, this key should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_facility_name(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention a facility name corresponding "
            "to this permit? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_facility_name", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_facility_name",
        prompt=(
            "What is the exact facility name as given in the permit text? "
            "Please do not normalize or shorten the name."
        ),
    )

    G.add_edge("get_facility_name", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "facility_name" and "explanation". The '
            'value of the "facility_name" key should be a string containing '
            "the facility name exactly as written in the permit. Please do "
            "not normalize or shorten the name."
            "If you could not determine a specific facility name, this key "
            "should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_facility_address(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention a street address for the "
            "facility corresponding to this permit? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_facility_address", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_facility_address",
        prompt=(
            "What is the facility street address as given in the permit? "
            "Please do not try to normalize the address."
        ),
    )

    G.add_edge("get_facility_address", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "facility_address" and "explanation". The '
            'value of the "facility_address" key should be a string '
            "containing the facility street address exactly as "
            "written in the permit. "
            "If you could not determine a specific facility street address "
            "corresponding to this permit, this key should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_county_name(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention a county name corresponding "
            "to this permit? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_county_name", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_county_name",
        prompt=(
            "What is the county name exactly as written in the permit? "
            "Please do not normalize or shorten the name."
        ),
    )
    G.add_edge("get_county_name", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "county_name" and "explanation". The '
            'value of the "county_name" key should be a string containing '
            "the county name, if unambiguous. "
            "If you could not determine a specific county name, this key "
            "should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_state_name(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention a state name corresponding "
            "to this permit? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_state_name", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_state_name",
        prompt="What is the state name exactly as written in the permit?",
    )
    G.add_edge("get_state_name", "get_state_abbr")
    G.add_node(
        "get_state_abbr",
        prompt="What is the two-letter abbreviation for this state?",
    )
    G.add_edge("get_state_abbr", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "state_abbr" and "explanation". The '
            'value of the "state_abbr" key should be a string containing '
            "the two-letter abbreviation for the state corresponding "
            "to this permit, if unambiguous. "
            "If you could not determine a specific state, this key "
            "should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_construction_notification(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** "
            "a notification of the construction commencement date? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "final_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"construction_notification_required" and "explanation". The '
            'value of the "construction_notification_required" key should '
            "be `true`, since we determined that the permit requires "
            "notification of the construction commencement date. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    G.add_edge(
        "init",
        "explicit_not_mentioned",
        condition=llm_response_starts_with_no,
    )
    G.add_node(
        "explicit_not_mentioned",
        prompt=(
            "Does the permit text **directly mention** that a notification "
            "of the construction commencement date is **not** required? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
        ),
    )
    G.add_edge(
        "explicit_not_mentioned",
        "final_not_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_not_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"construction_notification_required" and "explanation". The '
            'value of the "construction_notification_required" key should '
            "be `false`, since we determined that the permit explicitly does "
            "not require notification of the construction commencement date. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_construction_notification_window(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** "
            "a notification of the construction commencement date? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "check_for_window",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_for_window",
        prompt=(
            "Does the permit text **directly specify** a number of days "
            "within which construction commencement must be reported "
            "(e.g., 30)?"
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
        ),
    )

    G.add_edge(
        "check_for_window", "final", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"construction_notification_window" and "explanation". The '
            'value of the "construction_notification_window" key should '
            "an integer corresponding to the number of days within which "
            "construction commencement must be reported, if unambiguous. "
            "If you could not determine a specific number of days, this key "
            "should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_startup_notification(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** "
            "a notification of an initial startup date? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "final_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"startup_notification_required" and "explanation". The '
            'value of the "startup_notification_required" key should '
            "be `true`, since we determined that the permit requires "
            "a notification of an initial startup date. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    G.add_edge(
        "init",
        "explicit_not_mentioned",
        condition=llm_response_starts_with_no,
    )
    G.add_node(
        "explicit_not_mentioned",
        prompt=(
            "Does the permit text **directly mention** that a notification "
            "of an initial startup date is **not** required? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
        ),
    )
    G.add_edge(
        "explicit_not_mentioned",
        "final_not_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_not_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"startup_notification_required" and "explanation". The '
            'value of the "startup_notification_required" key should '
            "be `false`, since we determined that the permit explicitly does "
            "not require notification of an initial startup date. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_startup_notification_window(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** "
            "a notification of an initial startup date? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "check_for_window",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_for_window",
        prompt=(
            "Does the permit text **directly specify** a number of days "
            "within which the initial startup date must be reported "
            "(e.g., 15)?"
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
        ),
    )

    G.add_edge(
        "check_for_window", "final", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"startup_notification_window" and "explanation". The '
            'value of the "startup_notification_window" key should '
            "an integer corresponding to the number of days within which "
            "the initial startup date must be reported, if unambiguous. "
            "If you could not determine a specific number of days, this key "
            "should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_permit_copy_required(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** the facility "
            "to keep a copy of the permit onsite? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "final_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"permit_copy_required" and "explanation". The '
            'value of the "permit_copy_required" key should '
            "be `true`, since we determined that the permit requires "
            "that a copy of the permit be kept onsite. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    G.add_edge(
        "init",
        "explicit_not_mentioned",
        condition=llm_response_starts_with_no,
    )
    G.add_node(
        "explicit_not_mentioned",
        prompt=(
            "Does the permit text **directly mention** that the facility is "
            "**not** required to keep a copy of the permit onsite? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
        ),
    )
    G.add_edge(
        "explicit_not_mentioned",
        "final_not_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_not_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"permit_copy_required" and "explanation". The '
            'value of the "permit_copy_required" key should '
            "be `false`, since we determined that the permit explicitly does "
            "not require the facility to keep a copy of the permit onsite. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_roe_clause(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text mention any right-of-entry "
            "language enabling inspection? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_roe_clause", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_roe_clause",
        prompt=(
            "What is the direct text excerpt containing right-of-entry "
            "language enabling inspection? Be sure to include any and all "
            "language around timing (e.g., 'whenever the facility is in "
            "operation')."
        ),
    )

    G.add_edge("get_roe_clause", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "roe_clause" and "explanation". The '
            'value of the "roe_clause" key should be a string containing '
            "the right-of-entry language excerpt from before. Make sure the "
            "excerpt comes directly from the original text - do not "
            "paraphrase, expand upon, or generally make any changes to the "
            "original text. "
            "If you could not find any specific right-of-entry language "
            "enabling inspection, this key should be `null`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_generators(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention at least one backup generator "
            "in the application?"
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_refs", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_refs",
        prompt=(
            "Does the text provide a reference number or identifier for each "
            "backup generator mentioned "
            "(e.g., 'EG01', 'EG04-EG05', '1510-4')? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge("get_refs", "list_refs", condition=llm_response_starts_with_yes)

    G.add_node(
        "list_refs",
        prompt=(
            "Please list out all reference numbers or identifiers for each "
            "backup generator mentioned exactly as they appear in the permit. "
            "Do not consolidate generator sets even if they share make/model. "
            "List out the identifiers in the order they appear in the "
            "document."
        ),
    )

    G.add_edge("list_refs", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "reference_numbers" and "explanation". The '
            'value of the "reference_numbers" key should be the list of all '
            "backup generator identifiers mentioned in the text, as "
            "determined previously. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_num_gens(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the number of engines **for "
            "the generator with reference number {ref_number}**? Keep in "
            "mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_count", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_count",
        prompt=(
            "What is the number of engines for the generator with reference "
            "number {ref_number}?"
        ),
    )

    G.add_edge("get_count", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"engine_count" and "explanation". The '
            'value of the "engine_count" key should be an integer '
            "representing the number of engines for the generator with "
            "reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_included_in_permit(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text **directly specify** that the "
            "generator with reference number {ref_number} has been "
            "previously permitted? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )
    G.add_edge(
        "init", "final_prev_permitted", condition=llm_response_starts_with_yes
    )
    G.add_edge(
        "init", "is_current_permit", condition=llm_response_starts_with_no
    )

    G.add_node(
        "final_prev_permitted",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"included_in_permit_project" and "explanation". The '
            'value of the "included_in_permit_project" key should '
            "be `false`, since we determined that the generator with "
            "reference number {ref_number} has been previously permitted. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    G.add_node(
        "is_current_permit",
        prompt=(
            "Does the permit text **directly specify** that the "
            "generator with reference number {ref_number} is included in "
            "this permitting action (i.e. **not** previously permitted)? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
        ),
    )

    G.add_edge(
        "is_current_permit",
        "final",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"included_in_permit_project" and "explanation". The '
            'value of the "included_in_permit_project" key should be `true`, '
            "since we determined that the generator with reference number "
            "{ref_number} is explicitly included in this permitting action. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_make(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the manufacturer (e.g. "
            "Caterpillar, Cummins) for the generator with reference "
            "number {ref_number}? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_make", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_make",
        prompt=(
            "Who is the manufacturer of the generator with reference "
            "number {ref_number}? If multiple makes/models are listed for "
            "the same reference (e.g., 'Caterpillar 3516C or MTU 16V4000 "
            "DS2250 or equivalent'), select one deterministically: (1) "
            "prefer the option with the smallest stated electrical rating "
            "(kW); (2) if kW is not given for all, compare BHP and pick the "
            "smallest; (3) if still tied, pick the first explicitly named "
            "make. Report the chosen make verbatim as a single value. Do "
            "not include 'or', 'equivalent', or list multiple options. "
        ),
    )

    G.add_edge("get_make", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "make" and "explanation". The value of the '
            '"make" key should be a string containing the manufacturer of '
            "the generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer. Be sure to document any and al alternatives in the "
            '"explanation" text.'
        ),
    )

    return G


def setup_graph_model(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the model name or engine type "
            "for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number could be included in "
            "range or group of numbers and information that applies to that "
            "group should be considered relevant. "
            "The model name generally follows the manufacturer name. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_model", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_model",
        prompt=(
            "What is the model of the generator with reference number "
            "{ref_number}? If multiple models are listed for "
            "the same reference (e.g., 'Caterpillar 3516C or MTU 16V4000 "
            "DS2250 or equivalent'), select one deterministically: (1) "
            "prefer the option with the smallest stated electrical rating "
            "(kW); (2) if kW is not given for all, compare BHP and pick the "
            "smallest; (3) if still tied, pick the first explicitly named "
            "make. Report the chosen make verbatim as a single value. Do "
            "not include 'or', 'equivalent', or list multiple options. "
        ),
    )

    G.add_edge("get_model", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"model" and "explanation". The value of the "model" key should '
            "be a string containing the model of the generator with reference "
            "number {ref_number}. **Do not include the make**. "
            'The value of the "explanation" key should be a string explaining '
            "your answer. Be sure to document any and al alternatives in the "
            '"explanation" text.'
        ),
    )

    return G


def setup_graph_rated_capacity_kw(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text **directly specify** a numeric nominal "
            "nameplate capacity, in kW, **for the generator with reference "
            "number {ref_number}**? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "check_units", condition=llm_response_starts_with_yes)
    G.add_node(
        "check_units",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} directly specify capacity **in units of kW**? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_units",
        "check_distinguish",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_distinguish",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} distinguish between **nominal** and "
            "**maximum** capacity? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_distinguish",
        "cap_distinguish",
        condition=llm_response_starts_with_yes,
    )
    G.add_edge(
        "check_distinguish",
        "cap_no_distinguish",
        condition=llm_response_starts_with_no,
    )

    G.add_node(
        "cap_distinguish",
        prompt=(
            "We are interested in the **nominal** capacity for the generator "
            "with reference number {ref_number}. What is that capacity, in "
            "kW? If multiple kW values are listed for this generator, please "
            "give the smallest stated kW as a single number. Do not attempt "
            "to infer this value from other units."
        ),
    )

    G.add_node(
        "cap_no_distinguish",
        prompt=(
            "What is the capacity, in kW, for the generator with reference "
            "number {ref_number}? If multiple kW values are listed for this "
            "generator, please give the smallest stated kW as a single "
            "number. Do not attempt to infer this value from other units."
        ),
    )

    G.add_edge("cap_distinguish", "final")
    G.add_edge("cap_no_distinguish", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"rated_capacity_kw" and "explanation". The '
            'value of the "rated_capacity_kw" key should be an numerical '
            "value representing the nominal nameplate capacity, in kW, "
            "**for the generator with reference number {ref_number}**. "
            'The value of the "explanation" key should be a string explaining '
            "your answer. Document any ambiguities or multiple values in the "
            "'explanation' text."
        ),
    )

    return G


def setup_graph_rated_capacity_bhp(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text **directly specify** a numeric nominal "
            "nameplate capacity, in BHP, **for the generator with reference "
            "number {ref_number}**? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "check_units", condition=llm_response_starts_with_yes)
    G.add_node(
        "check_units",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} directly specify capacity **in units of BHP**? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_units",
        "check_distinguish",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_distinguish",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} distinguish between **nominal** and "
            "**maximum** capacity? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_distinguish",
        "cap_distinguish",
        condition=llm_response_starts_with_yes,
    )
    G.add_edge(
        "check_distinguish",
        "cap_no_distinguish",
        condition=llm_response_starts_with_no,
    )

    G.add_node(
        "cap_distinguish",
        prompt=(
            "We are interested in the **nominal** capacity for the generator "
            "with reference number {ref_number}. What is that capacity, in "
            "BHP? If multiple BHP values are listed for this generator, "
            "please give the smallest stated BHP as a single number. Do not "
            "attempt to infer this value from other units."
        ),
    )

    G.add_node(
        "cap_no_distinguish",
        prompt=(
            "What is the capacity, in BHP, for the generator with reference "
            "number {ref_number}? If multiple BHP values are listed for this "
            "generator, please give the smallest stated BHP as a single "
            "number. Do not attempt to infer this value from other units."
        ),
    )

    G.add_edge("cap_distinguish", "final")
    G.add_edge("cap_no_distinguish", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"rated_capacity_bhp" and "explanation". The '
            'value of the "rated_capacity_bhp" key should be an numerical '
            "value representing the nominal nameplate capacity, in BHP, "
            "**for the generator with reference number {ref_number}**. "
            'The value of the "explanation" key should be a string explaining '
            "your answer. Document any ambiguities or multiple values in the "
            "'explanation' text."
        ),
    )

    return G


def setup_graph_max_capacity_kw(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text **directly specify a maximum** "
            "capacity, in kW, **for the generator with reference "
            "number {ref_number}**? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "check_units", condition=llm_response_starts_with_yes)
    G.add_node(
        "check_units",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} directly specify maximum capacity **in units of "
            "kW**? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_units",
        "check_distinguish",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_distinguish",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} distinguish between **nominal** and "
            "**maximum** capacity? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_distinguish",
        "cap_distinguish",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "cap_distinguish",
        prompt=(
            "We are interested in the **maximum** capacity for the generator "
            "with reference number {ref_number}. What is that capacity, in "
            "kW? If multiple kW values are listed for this generator, please "
            "give the largest stated kW as a single number. Do not attempt "
            "to infer this value from other units."
        ),
    )

    G.add_edge("cap_distinguish", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"max_capacity_kw" and "explanation". The '
            'value of the "max_capacity_kw" key should be an numerical '
            "value representing the **maximum** nameplate capacity, in kW, "
            "**for the generator with reference number {ref_number}**. "
            'The value of the "explanation" key should be a string explaining '
            "your answer. Document any ambiguities or multiple values in the "
            "'explanation' text."
        ),
    )

    return G


def setup_graph_max_capacity_bhp(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text **directly specify a maximum** "
            "capacity, in BHP, **for the generator with reference "
            "number {ref_number}**? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "check_units", condition=llm_response_starts_with_yes)
    G.add_node(
        "check_units",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} directly specify maximum capacity **in units of "
            "BHP**? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_units",
        "check_distinguish",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_distinguish",
        prompt=(
            "Does the text for the generator with reference number "
            "{ref_number} distinguish between **nominal** and "
            "**maximum** capacity? "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
        ),
    )

    G.add_edge(
        "check_distinguish",
        "cap_distinguish",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "cap_distinguish",
        prompt=(
            "We are interested in the **maximum** capacity for the generator "
            "with reference number {ref_number}. What is that capacity, in "
            "BHP? If multiple BHP values are listed for this generator, "
            "please give the largest stated BHP as a single number. Do not "
            "attempt to infer this value from other units."
        ),
    )

    G.add_edge("cap_distinguish", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"max_capacity_bhp" and "explanation". The '
            'value of the "max_capacity_bhp" key should be an numerical '
            "value representing the **maximum** nameplate capacity, in BHP, "
            "**for the generator with reference number {ref_number}**. "
            'The value of the "explanation" key should be a string explaining '
            "your answer. Document any ambiguities or multiple values in the "
            "'explanation' text."
        ),
    )

    return G


# def setup_graph_capacity(**kwargs):  # noqa: D103
#     G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

#     G.add_node(
#         "init",
#         prompt=(
#             "Does the following text mention the rated capacity "
#             "for the generator(s) in the permit? "
#             "Begin your response with either 'Yes' or 'No' and explain "
#             "your answer."
#             '\n\n"""\n{text}\n"""'
#         ),
#     )

#     G.add_edge(
#         "init", "get_application", condition=llm_response_starts_with_yes
#     )

#     G.add_node(
#         "get_application",
#         prompt=(
#             "Does the capacity mentioned apply to multiple generators? "
#             "Begin your response with either 'Yes' or 'No' and explain "
#             "your answer."
#         ),
#     )

#     G.add_edge(
#         "get_application",
#         "get_permits",
#         condition=llm_response_starts_with_yes,
#     )

#     G.add_node(
#         "get_permits",
#         prompt=(
#             "What are the permit reference numbers that the capacity "
#             "applies to?"
#         ),
#     )

#     G.add_edge("get_permits", "check_permit")

#     G.add_node(
#         "check_permit",
#         prompt=(
#             "Does the capacity mentioned apply to the generator with "
#             "reference number {ref_number}?"
#         ),
#     )

#     G.add_edge(
#         "get_application",
#         "get_capacity_kw",
#         condition=llm_response_starts_with_no,
#     )
#     G.add_edge(
#         "check_permit",
#         "get_capacity_kw",
#         condition=llm_response_starts_with_yes,
#     )

#     # TODO: add check to ensure capacity is explicitly stated
#     # for this generator, not inferred
#     G.add_node(
#         "get_capacity_kw",
#         prompt=(
#             "What is the rated capacity of the generator with the reference "
#             "number {ref_number} in kilowatts (kW)?"
#         ),
#     )

#     G.add_edge("get_capacity_kw", "get_capacity_hp")

#     G.add_node(
#         "get_capacity_hp",
#         prompt=(
#             "What is the rated capacity of the generator with the reference "
#             "number {ref_number} in horsepower (HP)?"
#         ),
#     )

#     G.add_edge("get_capacity_hp", "final")

#     G.add_node(
#         "final",
#         prompt=(
#             "Respond based on our entire conversation so far. Return your "
#             "answer in JSON format (not markdown). Your JSON file must "
#             "include exactly three "
#             'keys. The keys are "capacity_kw", "capacity_hp", and '
#             '"explanation". The '
#             'value of the "capacity_kw" key should be a string containing '
#             "the rated capacity of the generator in kilowatts (kW). The "
#             'value of the "capacity_hp" key should be a string containing '
#             "the rated capacity of the generator in horsepower (HP). "
#             'The value of the "explanation" key should be a string explaining '
#             "your answer."
#         ),
#     )

#     return G


def setup_graph_fuel(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text specify the **primary** fuel type "
            "(e.g., 'diesel fuel', 'no. 2 distillate', 'ultra-low sulfur "
            "diesel', 'natural gas', 'propane', etc.) for the generator with "
            "reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_fuel_type", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_fuel_type",
        prompt=(
            "What type of fuel does the generator with reference number "
            "{ref_number} use?"
        ),
    )

    G.add_edge("get_fuel_type", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "fuel_type" and "explanation". The '
            'value of the "fuel_type" key should be a string containing '
            "the fuel type, exactly as written in the permit, of the "
            "generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_secondary_fuel(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text specify a **secondary** fuel type "
            "(i.e. for dual-fuel generators) for the generator with "
            "reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_fuel_type", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_fuel_type",
        prompt=(
            "What type of **secondary** fuel (e.g., 'diesel fuel', 'no. 2 "
            "distillate', 'ultra-low sulfur diesel', 'natural gas', "
            "'propane', etc.) does the generator with reference number "
            "{ref_number} use?"
        ),
    )

    G.add_edge("get_fuel_type", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "secondary_fuel_type" and "explanation". The '
            'value of the "secondary_fuel_type" key should be a string '
            "containing the **secondary** fuel type, exactly as written in "
            "the permit, of the generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_other_fuel(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text specify fuels **beyond** primary and "
            "secondary for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_fuel_type", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_fuel_type",
        prompt=(
            "What type of **other** fuels (e.g., 'diesel fuel', 'no. 2 "
            "distillate', 'ultra-low sulfur diesel', 'natural gas', "
            "'propane', etc.), **beyond** primary and secondary, "
            "does the generator with reference number {ref_number} use?"
        ),
    )

    G.add_edge("get_fuel_type", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "other_fuel_type" and "explanation". The '
            'value of the "other_fuel_types" key should be a string '
            "containing comma-separated names of other fuel types, "
            "**beyond** primary and secondary, exactly as listed in the "
            "permit for the generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_fuel_grade(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text specify the fuel grade (e.g., "
            "'Grade No. 2-D', 'Grade No. 1-D S15', etc.) for the "
            "generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_fuel_grade", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_fuel_grade",
        prompt=(
            "What is the fuel grade specified in the text for the "
            "generator with reference number {ref_number}?"
        ),
    )

    G.add_edge("get_fuel_grade", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "fuel_grade" and "explanation". The '
            'value of the "fuel_grade" key should be a string containing '
            "the fuel grade, exactly as written in the permit, for the "
            "generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_fuel_spec(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text cite a fuel specification standard "
            "(e.g., 'ASTM D975', 'ASTM D396', etc.) for the "
            "generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_fuel_spec", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_fuel_spec",
        prompt=(
            "What is the fuel specification standard cited in the text for "
            "the generator with reference number {ref_number}?"
        ),
    )

    G.add_edge("get_fuel_grade", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "fuel_spec" and "explanation". The '
            'value of the "fuel_spec" key should be a string containing '
            "the fuel grade, exactly as written in the permit, for the "
            "generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_fuel_sulphur(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text specify a fuel sulfur content "
            "(e.g., 0.0015 for 0.0015%, 15 ppm, etc.) for the "
            "generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain "
            "your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_sulfur", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_sulfur",
        prompt=(
            "What is the fuel sulfur content specified in the text for "
            "the generator with reference number {ref_number}? Give your "
            "answer as a percent regardless of how stated in permit. "
        ),
    )

    G.add_edge("get_sulfur", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "fuel_sulfur" and "explanation". The '
            'value of the "fuel_sulfur" key should be numerical value '
            "representing the fuel sulfur content **as a percent** for the "
            "generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_fuel_cert_required(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** "
            "a fuel supplier certification with each shipment? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "final_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"fuel_cert_required" and "explanation". The '
            'value of the "fuel_cert_required" key should '
            "be `true`, since we determined that the permit requires "
            "a fuel supplier certification with each shipment. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    G.add_edge(
        "init",
        "explicit_not_mentioned",
        condition=llm_response_starts_with_no,
    )
    G.add_node(
        "explicit_not_mentioned",
        prompt=(
            "Does the permit text **directly mention** that a fuel supplier "
            "certification is **not** required with each shipment? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
        ),
    )
    G.add_edge(
        "explicit_not_mentioned",
        "final_not_mentioned",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "final_not_mentioned",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two keys. The keys are "
            '"fuel_cert_required" and "explanation". The '
            'value of the "fuel_cert_required" key should '
            "be `false`, since we determined that the permit explicitly does "
            "not require a fuel supplier certification with each shipment. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_fuel_cert_fields(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following permit text **directly require** "
            "a fuel supplier certification with each shipment? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init",
        "check_supplier_name",
        condition=llm_response_starts_with_yes,
    )

    G.add_node(
        "check_supplier_name",
        prompt=(
            "Does the permit text **directly mention** that a fuel supplier "
            "name is required in the fuel supplier certification? "
        ),
    )
    G.add_edge("check_supplier_name", "check_receipt_date")
    G.add_node(
        "check_receipt_date",
        prompt=(
            "Does the permit text **directly mention** that receipt/delivery "
            "date is required in the fuel supplier certification? "
        ),
    )
    G.add_edge("check_receipt_date", "check_quantity")
    G.add_node(
        "check_quantity",
        prompt=(
            "Does the permit text **directly mention** that quantity/volume "
            "is required in the fuel supplier certification? "
        ),
    )
    G.add_edge("check_quantity", "check_astm")
    G.add_node(
        "check_astm",
        prompt=(
            "Does the permit text **directly mention** that an ASTM "
            "compliance statement is required in the fuel supplier "
            "certification? "
        ),
    )
    G.add_edge("check_astm", "check_sulfur")
    G.add_node(
        "check_sulfur",
        prompt=(
            "Does the permit text **directly mention** that sulfur content "
            "is required in the fuel supplier certification? "
        ),
    )
    G.add_edge("check_sulfur", "final")
    # G.add_node(
    #     "final",
    #     prompt=(
    #         "Respond based on our entire conversation so far. Return your "
    #         "answer in JSON format (not markdown). Your JSON file must "
    #         "include exactly two keys. The keys are "
    #         '"fuel_cert_fields" and "explanation". The value of the '
    #         '"fuel_cert_required" key should  be another dictionary with '
    #         "be `false`, since we determined that the permit explicitly does "
    #         "not require a fuel supplier certification with each shipment. "
    #         'The value of the "explanation" key should be a string explaining '
    #         "your answer."
    #     ),
    # )

    return G


# def setup_graph_tank_size(**kwargs):  # noqa: D103
#     G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

#     G.add_node(
#         "init",
#         prompt=(
#             "Does the following text mention the tank size (e.g., 500 "
#             "gallons) "
#             "for the generator with reference number {ref_number}? "
#             "Keep in mind that the reference number "
#             "could be included in range or group of numbers and information "
#             "that applies to that group should be considered relevant. "
#             "Begin your response with either 'Yes' or 'No' and explain "
#             "your answer."
#             '\n\n"""\n{text}\n"""'
#         ),
#     )

#     G.add_edge("init", "get_tank_size", condition=llm_response_starts_with_yes)

#     G.add_node(
#         "get_tank_size",
#         prompt=(
#             "What is the tank size of the generator with reference number "
#             "{ref_number}? "
#             "Include the units (e.g., gallons, liters) in your answer."
#         ),
#     )

#     G.add_edge("get_tank_size", "get_max_duration")

#     G.add_node(
#         "get_max_duration",
#         prompt=(
#             "Does the text specify an expected max duration without refueling "
#             "for the generator with reference number {ref_number}? "
#             "If so, what is that duration (include units, e.g., hours, days)?"
#         ),
#     )

#     G.add_edge("get_max_duration", "final")

#     G.add_node(
#         "final",
#         prompt=(
#             "Respond based on our entire conversation so far. Return your "
#             "answer in JSON format (not markdown). Your JSON file must "
#             "include exactly three "
#             'keys. The keys are "tank_size", "max_duration", and '
#             '"explanation". The value of the "tank_size" key should be a '
#             "string containing the tank size of the generator with reference number {ref_number}. "
#             'The value of the "max_duration" key should be a string '
#             "containing the max duration of the generator with reference number {ref_number}. "
#             'The value of the "explanation" key should be a string explaining '
#             "your answer."
#         ),
#     )

#     return G


def setup_graph_backup(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention how much back up generation, "
            "in megawatts (MW), is provided by the generator with reference "
            "number {ref_number}? Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_backup", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_backup",
        prompt=(
            "How much back up generation is provided by the generator with "
            "reference number {ref_number} in megawatts (MW)?"
            # TODO: model is doing math here,
            # should specify not to infer?
        ),
    )

    G.add_edge("get_backup", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "backup_mw" and "explanation". The '
            'value of the "backup_mw" key should be a string containing '
            "the amount of back up generation provided by the generator in "
            "megawatts (MW). "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_control_techs(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            # TODO: refine this prompt, what exactly are we looking for?
            # emissions contol techs? define what control techs are?
            "Does the following text mention control technologies "
            "for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_techs", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_techs",
        prompt=(
            "What control technologies are associated with the generator "
            "with reference number {ref_number}?"
        ),
    )

    G.add_edge("get_techs", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "control_technologies" and "explanation". The '
            'value of the "control_technologies" key should be a string '
            "containing the control technologies associated with the "
            "generator in question. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_operating_hours(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention annual operating hours limits "
            "for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_hours", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_hours",
        prompt=(
            "How many hours, per year, is the generator with reference "
            "number {ref_number} allowed to operate?"
        ),
    )

    G.add_edge("get_hours", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "operating_hours" and "explanation". The '
            'value of the "operating_hours" key should be an integer '
            "containing "
            "the operating hours associated with the generator with reference number {ref_number}. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_emissions(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention emissions limits "
            "for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_pollutants", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_pollutants",
        prompt=(
            "Does the text mention emissions limits for multiple pollutants? "
            "If so, what are the pollutants mentioned?"
        ),
    )

    G.add_edge("get_pollutants", "get_units")

    G.add_node(
        "get_units",
        prompt=(
            "What units are used for the emissions limits mentioned in the "
            "text?"
        ),
    )

    G.add_edge("get_units", "get_limits")

    G.add_node(
        "get_limits",
        prompt=(
            "What are the emissions limits for the generator with "
            "reference number {ref_number}?"
        ),
    )

    G.add_edge("get_limits", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "emissions_limits" and "explanation". The '
            'value of the "emissions_limits" key should be a dictionary with '
            "subkeys for each pollutant mentioned and their corresponding "
            "limits. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G
