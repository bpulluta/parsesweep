"""ELM Ordinance Decision Tree Graph setup functions."""

import networkx as nx
from elm.ords.extraction.graphs import llm_response_starts_with_no, llm_response_starts_with_yes


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
    return nx.DiGraph(SECTION_PROMPT=_SECTION_PROMPT, COMMENT_PROMPT=_COMMENT_PROMPT, **kwargs)


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

    G.add_edge("init", "get_permit_num", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_permit_num",
        prompt=("What is the permit or registration number mentioned in the text?"),
    )

    G.add_edge("get_permit_num", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "permit_number" and "explanation". The '
            'value of the "permit_number" key should be a string containing '
            "the permit or registration number mentioned in the text. The "
            'the value of the "explanation" key should be a string explaining '
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
            "backup generator mentioned? Begin your response with either 'Yes' "
            "or 'No' and explain your answer."
        ),
    )

    G.add_edge("get_refs", "final", condition=llm_response_starts_with_yes)

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "reference_numbers" and "explanation". The '
            'value of the "reference_numbers" key should be a list containing '
            "the identifiers of all backup generators mentioned in the text. The "
            'the value of the "explanation" key should be a string explaining '
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
            "Caterpillar, Cummins)for the generator with reference "
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
        prompt=("Who is the manufacturer of the generator with reference number {ref_number}?"),
    )

    G.add_edge("get_make", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "make" and "explanation". The '
            'value of the "make" key should be a string containing '
            "the manufacturer of the generator in question. The "
            'the value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_model(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the model name "
            "or engine type for the generator with reference "
            "number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "The model name generally follows the "
            "manufacturer name. Begin your response with either "
            "'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_model", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_model",
        prompt=("What is the model of the generator with reference number {ref_number}?"),
    )

    G.add_edge("get_model", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "model" and "explanation". The '
            'value of the "model" key should be a string containing '
            "the model of the generator in question, do not include the make. The "
            'the value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_fuel(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the fuel type (e.g., diesel, natural gas, "
            "distillate oil, etc.) for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_fuel_type", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_fuel_type",
        prompt=("What type of fuel does the generator with reference number {ref_number} use?"),
    )

    G.add_edge("get_fuel_type", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "fuel_type" and "explanation". The '
            'value of the "fuel_type" key should be a string containing '
            "the fuel type of the generator in question. The "
            'the value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_tank_size(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the tank size (e.g., 500 gallons) "
            "for the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_tank_size", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_tank_size",
        prompt=(
            "What is the tank size of the generator with reference number {ref_number}?"
            "Include the units (e.g., gallons, liters) in your answer."
        ),
    )

    G.add_edge("get_tank_size", "get_max_duration")

    G.add_node(
        "get_max_duration",
        prompt=(
            "Does the text specify an expected max duration without refueling "
            "for the generator with reference number {ref_number}? "
            "If so, what is that duration (include units, e.g., hours, days)?"
        ),
    )

    G.add_edge("get_max_duration", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly three "
            'keys. The keys are "tank_size", "max_duration", and "explanation". The '
            'value of the "tank_size" key should be a string containing '
            "the tank size of the generator in question. The "
            'value of the "max_duration" key should be a string containing '
            "the max duration of the generator in question. The "
            'the value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_capacity(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention the rated capacity "
            "for the generator(s) in the permit? "
            "Begin your response with either 'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_application", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_application",
        prompt=(
            "Does the capacity mentioned apply to multiple generators? "
            "Begin your response with either 'Yes' or 'No' and explain your answer."
        ),
    )

    G.add_edge("get_application", "get_permits", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_permits",
        prompt=("What are the permit reference numbers that the capacity applies to?"),
    )

    G.add_edge("get_permits", "check_permit")

    G.add_node(
        "check_permit",
        prompt=(
            "Does the capacity mentioned apply to the generator with reference number {ref_number}?"
        ),
    )

    G.add_edge("get_application", "get_capacity_kw", condition=llm_response_starts_with_no)
    G.add_edge("check_permit", "get_capacity_kw", condition=llm_response_starts_with_yes)

    # TODO: add check to ensure capacity is explicitly stated
    # for this generator, not inferred
    G.add_node(
        "get_capacity_kw",
        prompt=(
            "What is the rated capacity of the generator with the reference "
            "number {ref_number} in kilowatts (kW)?"
        ),
    )

    G.add_edge("get_capacity_kw", "get_capacity_hp")

    G.add_node(
        "get_capacity_hp",
        prompt=(
            "What is the rated capacity of the generator with the reference "
            "number {ref_number} in horsepower (HP)?"
        ),
    )

    G.add_edge("get_capacity_hp", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly three "
            'keys. The keys are "capacity_kw", "capacity_hp", and "explanation". The '
            'value of the "capacity_kw" key should be a string containing '
            "the rated capacity of the generator in kilowatts (kW). The "
            'value of the "capacity_hp" key should be a string containing '
            "the rated capacity of the generator in horsepower (HP). The "
            'the value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G


def setup_graph_backup(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention how much back up generation, "
            "in megawatts (MW), is provided by the generator with reference number {ref_number}? "
            "Keep in mind that the reference number "
            "could be included in range or group of numbers and information "
            "that applies to that group should be considered relevant. "
            "Begin your response with either 'Yes' or 'No' and explain your answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge("init", "get_backup", condition=llm_response_starts_with_yes)

    G.add_node(
        "get_backup",
        prompt=(
            "How much back up generation is provided by the generator with reference number "
            "{ref_number} in megawatts (MW)?"
            # TODO: model is doing math here,
            # should specify not to infer?
        ),
    )

    G.add_edge("get_backup", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "backup_mw" and "explanation". The '
            'value of the "backup_mw" key should be a string containing '
            "the amount of back up generation provided by the generator in megawatts (MW). The "
            'the value of the "explanation" key should be a string explaining '
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
            "generator in question. The "
            'the value of the "explanation" key should be a string explaining '
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
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "operating_hours" and "explanation". The '
            'value of the "operating_hours" key should be an integer containing '
            "the operating hours associated with the generator in question. The "
            'the value of the "explanation" key should be a string explaining '
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

    G.add_edge("init", "get_pollutants", condition=llm_response_starts_with_yes)

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
        prompt=("What units are used for the emissions limits mentioned in the text?"),
    )

    G.add_edge("get_units", "get_limits")

    G.add_node(
        "get_limits",
        prompt=(
            "What are the emissions limits for the generator with reference number {ref_number}?"
        ),
    )

    G.add_edge("get_limits", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must include exactly two "
            'keys. The keys are "emissions_limits" and "explanation". The '
            'value of the "emissions_limits" key should be a dictionary with '
            "subkeys for each pollutant mentioned and their corresponding limits. The "
            'the value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G
