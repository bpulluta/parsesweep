#!/bin/sh
set -eu

KEY=${1:?Usage: $0 KEY DESCRIPTION}
DESC=${2:?Usage: $0 KEY DESCRIPTION}

cat <<EOF
def setup_graph_${KEY}(**kwargs):  # noqa: D103
    G = _setup_graph_no_nodes(**kwargs)  # noqa: N806

    G.add_node(
        "init",
        prompt=(
            "Does the following text mention an ${DESC} "
            "corresponding to this permit? "
            "Begin your response with either 'Yes' or 'No' and explain your "
            "answer."
            '\n\n"""\n{text}\n"""'
        ),
    )

    G.add_edge(
        "init", "get_${KEY}", condition=llm_response_starts_with_yes
    )

    G.add_node(
        "get_${KEY}",
        prompt="What is the stated ${DESC}?",
    )

    G.add_edge("get_${KEY}", "final")

    G.add_node(
        "final",
        prompt=(
            "Respond based on our entire conversation so far. Return your "
            "answer in JSON format (not markdown). Your JSON file must "
            "include exactly two "
            'keys. The keys are "${KEY}" and "explanation". The '
            'value of the "${KEY}" key should be a string containing '
            "the ${DESC}, if unambiguous. "
            "If you could not determine a specific ${DESC}, this key "
            "should be \`null\`. "
            'The value of the "explanation" key should be a string explaining '
            "your answer."
        ),
    )

    return G
EOF
