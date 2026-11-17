"""``psweep validate`` end to end: real extract, compile and alignment; fake LLM only."""

from __future__ import annotations

import copy
import csv
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from psweep.cli.app import cli
from psweep.discovery.targets import identify_target
from psweep.extraction.roster import Roster
from psweep.utils.evidence import normalize
from psweep.utils.file_io import publication_lock
from psweep.validation import covered_keys

FIXTURES = Path(__file__).parent / "fixtures" / "validate"
URL = re.compile(r"SOURCE_URL: (\S+)")


class FakeLLM:
    """Canned extraction payloads per (model, source URL); a scripted judge."""

    def __init__(self):
        self.payloads: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.items: list[dict] = []
        self.verdict = None

    def __call__(self, client, text=None, schema=None, **kwargs):
        if "verdicts" in (schema or {}).get("properties", {}):
            self.calls.append((client.model, "judge"))
            items = [json.loads(line) for line in text.splitlines()]
            self.items += items
            verdicts = [self.verdict(item) for item in items]
            return {
                "data": {"verdicts": verdicts},
                "cost": 0.001,
                "input_tokens": 10,
                "output_tokens": 5,
            }
        url = URL.search(text).group(1)
        self.calls.append((client.model, url))
        return copy.deepcopy(self.payloads[(client.model, url)])

    def extraction_calls(self, model=None):
        return [c for c in self.calls if c[1] != "judge" and model in {None, c[0]}]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = {"provider": "openai", "model": "gpt-fixture", "api_key": "fixture"}
    monkeypatch.setattr("psweep.utils.config.get_config", lambda: SimpleNamespace(llm_config=cfg))
    llm = FakeLLM()
    llm.config = cfg
    monkeypatch.setattr(
        "psweep.extraction.llm_client.LLMClient.extract",
        lambda client, *args, **kwargs: llm(client, *args, **kwargs),  # noqa: PLW0108 - must bind as a method
    )
    return llm


def psweep(*args, code=0):
    result = CliRunner().invoke(cli, list(args), catch_exceptions=False)
    assert result.exit_code == code, result.output
    return result.output


def write_index(domain: str, rows: list[dict], fields: list[str]) -> None:
    discovered = Path("outputs", domain, "discovered")
    with (discovered / "download_index.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "relative_path",
                "source_scope",
                "target_id",
                "target_metadata",
                "url",
            ],
        )
        writer.writeheader()
        for row in rows:
            meta = json.loads(row["target_metadata"]) if row["target_metadata"] else None
            writer.writerow(
                {
                    **row,
                    "path": (discovered / "curated" / row["relative_path"]).resolve().as_posix(),
                    "target_id": identify_target(meta, fields) if meta else "",
                }
            )


def read_csv(path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def summary(domain: str) -> dict:
    return json.loads(
        Path("outputs", domain, "validated", "summary.json").read_text(encoding="utf-8")
    )


# ── S1: validation of a solartrace primary against its human-labelled sheet ──


@pytest.fixture
def solartrace(fake):
    """The fixture domain is read in place; every output lands in the tmp cwd."""
    domain = FIXTURES / "solartrace"
    shutil.copytree(domain / "curated", "outputs/solartrace/discovered/curated")
    write_index("solartrace", read_csv(domain / "index.csv"), ["state", "ahj"])
    responses = json.loads((domain / "responses.json").read_text())
    fake.payloads = {(fake.config["model"], url): payload for url, payload in responses.items()}
    config = str(domain / "run.yaml")
    psweep("extract", "--config", config, "-q")
    psweep("compile", "--config", config, "-q")
    fake.calls.clear()
    return config


def test_s1_primary_against_human_sheet(solartrace, fake):
    fake.config["api_key"] = ""  # nothing pending and no models: no credentials needed
    output = psweep("validate", "--config", solartrace)
    assert fake.calls == []
    cells = read_csv("outputs/solartrace/validated/cells.csv")
    assert len(cells) == 27
    statuses = [c["status"] for c in cells]
    assert {s: statuses.count(s) for s in set(statuses)} == {
        "unanimous": 5,
        "partial": 1,
        "reference_only": 19,
        "all_na": 2,
    }
    (new,) = [c for c in cells if c["status"] == "partial"]
    assert (new["AHJ Name"], new["column"]) == (
        "Maricopa County",
        "Third-Party Permitting Available",
    )
    assert "SolarAPP+" in new["primary_quote"] and "maricopa.gov" in new["primary_url"]
    assert [c["miss"] for c in cells if c["status"] == "reference_only"] == ["extraction"] * 19
    priority = [c["review_priority"] for c in cells]
    assert (priority.count("medium"), priority.count("low"), priority.count("none")) == (1, 24, 2)
    assert len(read_csv("outputs/solartrace/validated/summary.csv")) == 10
    data = summary("solartrace")
    overall = data["models"]["primary"]["overall"]["ahj_requirements"]
    assert (overall["coverage"], overall["accuracy"], overall["correct"]) == (0.2222, 0.2593, 7)
    assert overall["ci95"]["accuracy"] is None  # cells within targets are dependent
    assert (overall["precision"], overall["recall"], overall["f1"]) == (0.8333, 0.2083, 0.3333)
    assert (overall["filled"], overall["tp"], overall["fp"], overall["fn"]) == (6, 5, 1, 19)
    # The sheet has 6 data rows; 3 are in the roster.
    assert data["rows"]["out_of_scope"] == {"ahj_requirements": 3}
    assert data["diagnosis"]["no_docs"]["count"] == 0
    assert "gate" not in data and "Gate:" not in output
    assert "primary  0.26" in output and "Next: extraction" in output
    assert "±" not in output and "all-answered target correctness" in output
    assert not Path("outputs/solartrace/validated/.staging").exists()


# ── Synthetic roster domain: targets t1..tN, one curated document each ──

SITES = ["Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Gum", "Hazel", "Ivy", "Juniper"]
TEXT = {"x": "number", "y": "string", "z": "string"}
MODELS = {"primary": "m-p", "a": "m-a", "b": "m-b", "c": "m-c", "d": "m-d", "judge": "m-judge"}


def site(t: int) -> str:
    return f"{SITES[t - 1]} Park"


def url(t: int) -> str:
    return f"https://bench.example/t{t}"


def quote(t: int, field: str, value) -> str:
    return f"{site(t)} {field} is {value}."


def bench_schema(fields, descriptions=None) -> dict:
    descriptions = descriptions or {}
    item = {
        "state": {"type": "string", "title": "State"},
        "site": {"type": "string", "title": "Site"},
        **{
            f: {
                "title": f.upper(),
                "description": descriptions.get(f, f"{f} of the site"),
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "quote"],
                "properties": {
                    "value": {"type": [TEXT[f], "null"]},
                    "quote": {"type": ["string", "null"]},
                },
            }
            for f in fields
        },
    }
    return {
        "type": "object",
        "required": ["sites"],
        "additionalProperties": False,
        "$metadata": {
            "extraction": {"main_data_array": "sites"},
            "identity": {"deduplication": {"key_fields": ["state", "site"]}},
        },
        "properties": {
            "sites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(item),
                    "properties": item,
                },
            }
        },
    }


class Bench:
    """Writes the ``bench`` domain and scripts canned payloads per model."""

    def __init__(self, fake, targets: int, fields=("x",)):
        self.fake, self.fields, self.config = fake, list(fields), Path("config/bench/run.yaml")
        self.targets = list(range(1, targets + 1))
        self.values: dict[str, dict[int, dict]] = {}
        self.extra_quotes: dict[int, list[str]] = {}
        self.config.parent.mkdir(parents=True)
        with Path("config/bench/targets.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["state", "site", "region"])
            writer.writerows(["CO", site(t), f"r{t % 3}"] for t in self.targets)
        self.write_schema()
        self.write_run(None)

    def write_schema(self, descriptions=None):
        Path("config/bench/schema.json").write_text(
            json.dumps(bench_schema(self.fields, descriptions), indent=1), encoding="utf-8"
        )

    def write_run(self, validation: dict, **extra):
        run = {
            "domain": "bench",
            "models": MODELS,
            "discovery": {
                "targets": "targets.csv",
                "queries": ["{site} {state}"],
                "partition_by": ["state", "site"],
            },
            "extraction": {"model": "primary"},
            "compilation": {"output": {"default_format": "csv"}},
            **extra,
        }
        if validation is not None:
            run["validation"] = validation
        self.config.write_text(yaml.safe_dump(run, sort_keys=False))

    def write_docs(self, targets=None):
        curated = Path("outputs/bench/discovered/curated")
        rows = []
        for t in targets or self.targets:
            quotes = {
                quote(t, f, v)
                for model in self.values.values()
                for f, v in model.get(t, {}).items()
                if v is not None
            } | set(self.extra_quotes.get(t, []))
            path = curated / f"t{t}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{site(t)}, Colorado\n\n" + "\n".join(sorted(quotes)) + "\n")
            meta = {"state": "CO", "site": site(t), "region": f"r{t % 3}"}
            rows.append(
                {
                    "relative_path": path.name,
                    "source_scope": "target",
                    "url": url(t),
                    "target_metadata": json.dumps(meta),
                }
            )
        write_index("bench", rows, ["state", "site"])

    def script(self, model: str, values: dict[int, dict]):
        """``values``: target -> {field: value | None}; canned with quotes from the document."""
        self.values[model] = values
        for t in self.targets:
            row = {"state": "CO", "site": site(t)}
            for f in self.fields:
                v = values.get(t, {}).get(f)
                row[f] = {"value": v, "quote": None if v is None else quote(t, f, v)}
            self.fake.payloads[(MODELS[model], url(t))] = {
                "data": {"sites": [row]},
                "cost": 0.01,
                "input_tokens": 1000,
                "output_tokens": 100,
            }

    def extract_compile(self):
        psweep("extract", "--config", str(self.config), "-q")
        psweep("compile", "--config", str(self.config), "-q")

    def validate(self, *args, code=0):
        return psweep("validate", "--config", str(self.config), *args, code=code)


def judge_by(supported: set[str], same: frozenset[str] = frozenset()):
    """Verdicts grouping by value (one group for ``same`` fields); supported iff quote listed."""

    def verdict(item):
        groups: dict = {}
        return {
            "id": item["id"],
            "candidates": [
                {
                    "id": c["id"],
                    "support": "supported" if c["quote"] in supported else "unsupported",
                    "group": 0
                    if item["field"] in same
                    else groups.setdefault(c["value"], len(groups)),
                }
                for c in item["candidates"]
            ],
            "reason": "fixture",
        }

    return verdict


S2 = {  # target -> P, A, B, C, D values of x (None = NA)
    1: (10, 10, 10, 10, 10),
    2: (10, 10, 10, 12, 10),
    3: (None, 5, 5, 5, None),
    4: (7, None, None, None, None),
}


@pytest.fixture
def s2(fake):
    bench = Bench(fake, 4)
    for i, model in enumerate(("primary", "a", "b", "c", "d")):
        bench.script(model, {t: {"x": row[i]} for t, row in S2.items()})
    bench.write_docs()
    bench.extract_compile()
    fake.verdict = judge_by({quote(2, "x", 10), quote(3, "x", 5), quote(4, "x", 7)})
    return bench


OUTPUTS = ("cells.csv", "summary.csv", "summary.json")


def outputs(domain: str = "bench") -> dict[str, object]:
    """Validate outputs without per-run accounting (timings, call and cache counters)."""
    root = Path("outputs", domain, "validated")
    data = {name: (root / name).read_bytes() for name in OUTPUTS[:2]}
    data["summary.json"] = {k: v for k, v in summary(domain).items() if k not in {"run", "judge"}}
    for model in data["summary.json"]["models"].values():
        model["quality"].pop("seconds_per_document")
        model["quality"].pop("seconds_per_target")
    return data


def metrics(data: dict, label: str, base: str = "consensus", column: str | None = None) -> dict:
    model = data["models"][label]
    return (model["columns"][column] if column else model["overall"])[base]


def test_s2_five_contenders_no_reference(s2, fake):
    s2.write_run({"models": ["a", "b", "c", "d"], "judge": "judge"})
    fake.calls.clear()
    output = s2.validate()
    assert {m for m, _ in fake.extraction_calls()} == {"m-a", "m-b", "m-c", "m-d"}
    assert fake.calls.count(("m-judge", "judge")) == 1
    data = summary("bench")
    expected = {  # label: (P, R, F1, split, support_rate)
        "primary": (0.6667, 0.6667, 0.6667, 0, 1.0),
        "a": (1.0, 1.0, 1.0, 1, 1.0),
        "b": (1.0, 1.0, 1.0, 1, 1.0),
        "c": (0.5, 0.5, 0.5, 1, 0.5),
        "d": (1.0, 0.6667, 0.8, 0, 1.0),
    }
    for label, want in expected.items():
        m = metrics(data, label)
        got = (m["precision"], m["recall"], m["f1"], m["split"], m["support_rate"])
        assert got == want, label
    p = metrics(data, "primary")
    assert (p["tp"], p["fp"], p["fn"]) == (2, 1, 1)
    assert (p["precision_j"], p["recall_j"], p["f1_j"]) == (1.0, 0.6667, 0.8)
    pairs = data["agreement"]["overall"]
    assert pairs["primary|c"]["rate"] == 0.5
    assert (pairs["primary|a"]["rate"], pairs["primary|a"]["one_sided"]) == (1.0, 2)
    priority = {
        c["Site"]: c["review_priority"] for c in read_csv("outputs/bench/validated/cells.csv")
    }
    assert priority == {site(1): "low", site(2): "low", site(3): "high", site(4): "low"}
    assert data["run"]["llm_calls"] == {"a": 4, "b": 4, "c": 4, "d": 4, "judge": 1}
    assert "Next: review" in output

    before = outputs()
    fake.calls.clear()
    s2.validate()
    assert fake.calls == []
    assert outputs() == before


def test_s6_add_a_model(s2, fake):
    s2.write_run({"models": ["a", "b", "c"], "judge": "judge"})
    fake.calls.clear()
    s2.validate()
    assert {m for m, _ in fake.extraction_calls()} == {"m-a", "m-b", "m-c"}
    assert fake.calls.count(("m-judge", "judge")) == 1

    s2.write_run({"models": ["a", "b", "c", "d"], "judge": "judge"})
    fake.calls.clear()
    s2.validate()
    assert {m for m, _ in fake.calls} == {"m-d"}
    added = outputs()

    fake.calls.clear()
    s2.validate("--fresh")
    assert fake.calls.count(("m-judge", "judge")) == 1
    assert len(fake.extraction_calls()) == 16
    assert outputs() == added


def write_reference(name: str, rows: dict[int, dict], titles=("X", "Y", "Z")) -> str:
    path = Path("config/bench/samples", f"{name}.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["State", "Site", *titles])
        for t, values in rows.items():
            writer.writerow(["CO", site(t), *(values.get(title.lower(), "") for title in titles)])
    return f"samples/{name}.csv"


def s3_values(t: int, y: str = "ok") -> dict:
    return {"x": t, "y": y, "z": f"z{t}"}


@pytest.fixture
def s3(fake):
    bench = Bench(fake, 4, fields=("x", "y", "z"))
    for model, first_y in (("primary", "red"), ("a", "blue"), ("b", "red"), ("c", "red")):
        bench.script(model, {t: s3_values(t, first_y if t == 1 else "ok") for t in bench.targets})
    bench.write_docs()
    bench.extract_compile()
    human = {t: s3_values(t, "green" if t == 1 else "ok") for t in range(1, 10)}
    bench.references = [
        write_reference("h1", {t: human[t] for t in (1, 2, 3)}),
        write_reference("h2", {t: human[t] for t in (1, 2, 3, 4)}, titles=("X", "Y")),
        write_reference("h3", {t: human[t] for t in (2, 4, 9)}),
    ]
    fake.verdict = judge_by({quote(1, "y", "red")})
    return bench


S3_RUN = {"models": ["a", "b", "c"], "judge": "judge"}


def test_s3_models_and_human_sheets(s3, fake):
    s3.write_run({**S3_RUN, "references": s3.references})
    fake.calls.clear()
    output = s3.validate()
    assert "h2: lacks Z; unscored for this reference" in output
    data = summary("bench")
    assert len(read_csv("outputs/bench/validated/summary.csv")) == 4 * 4 * 4
    assert data["rows"]["out_of_scope"] == {"h1": 0, "h2": 0, "h3": 1}
    assert len(data["agreement"]["overall"]) == 21
    assert metrics(data, "primary", "h2", "Z")["compared"] == 0
    assert metrics(data, "primary", "h1")["compared"] == 9  # t4 is outside h1
    assert metrics(data, "primary", "h2")["compared"] == 8
    (item,) = fake.items
    assert fake.calls.count(("m-judge", "judge")) == 1
    candidates = {c["value"]: c["quote"] for c in item["candidates"]}
    assert candidates == {
        "red": quote(1, "y", "red"),
        "blue": quote(1, "y", "blue"),
        "green": None,
    }
    (cell,) = [
        c
        for c in read_csv("outputs/bench/validated/cells.csv")
        if (c["Site"], c["column"]) == (site(1), "Y")
    ]
    assert cell["judge_groups"] == "a | h1,h2 | primary,b,c"


def test_s4_focus_fields(s3, fake):
    s3.write_run({**S3_RUN, "references": s3.references})
    s3.validate()
    full = outputs()
    fake.calls.clear()
    s3.write_run({**S3_RUN, "references": s3.references, "fields": ["x", "y"]})
    s3.validate()
    assert fake.calls == []
    assert {c["column"] for c in read_csv("outputs/bench/validated/cells.csv")} == {"X", "Y"}
    rows = read_csv("outputs/bench/validated/summary.csv")
    assert len(rows) == 4 * 3 * 4
    data = summary("bench")
    star = metrics(data, "primary", "h2")
    assert star["compared"] == sum(metrics(data, "primary", "h2", c)["compared"] for c in "XY")

    s3.write_run({**S3_RUN, "references": s3.references})
    s3.validate()
    assert fake.calls == []
    assert outputs() == full


def test_s7_converge_to_min_accuracy(fake):
    bench = Bench(fake, 4, fields=("x", "y", "z"))
    truth = {t: s3_values(t) for t in bench.targets}
    validation = {"references": [write_reference("ex", truth)], "min_accuracy": 0.9}
    bench.write_run(validation)
    bench.script("primary", {**truth, 2: s3_values(2, "wrong")})
    bench.write_docs(targets=[1, 2, 3])
    bench.extract_compile()
    output = bench.validate(code=1)
    assert "primary accuracy 0.67 < min_accuracy 0.90 vs ex" in output
    assert "Next: extraction — Y 0 missed / 1 wrong · discovery — 1 targets" in output
    data = summary("bench")
    ex = metrics(data, "primary", "ex")
    assert (ex["correct"], ex["compared"], ex["ci95"]["accuracy"]) == (8, 12, None)
    assert data["gate"]["passed"] is False
    assert data["diagnosis"]["columns"]["Y"]["wrong"] == 1
    no_docs = [c for c in read_csv("outputs/bench/validated/cells.csv") if c["miss"] == "no_docs"]
    assert {c["Site"] for c in no_docs} == {site(4)} and len(no_docs) == 3

    shutil.rmtree("outputs/bench/validated")  # no LLM call, so `run` can host this stage
    run = psweep("run", "--config", str(bench.config), "--skip-discover", "--yes", code=1)
    assert "Stage 'validate' failed (exit 1)" in run
    assert summary("bench")["gate"]["passed"] is False

    bench.write_schema({"y": "y of the site, exactly as the plan states it"})
    bench.script("primary", truth)
    bench.write_docs()
    fake.calls.clear()
    bench.extract_compile()  # `run` stages are subprocesses, out of the fake's reach
    bench.validate()
    assert len(fake.extraction_calls("m-p")) == 4
    data = summary("bench")
    ex = metrics(data, "primary", "ex")
    assert (ex["correct"], ex["compared"], ex["ci95"]["accuracy"]) == (12, 12, None)
    assert data["gate"]["passed"] is True


def test_s8_sample_fill_score(fake):
    bench = Bench(fake, 10, fields=("x", "y", "z"))
    bench.script("primary", {t: s3_values(t) for t in bench.targets})
    bench.write_docs()
    bench.extract_compile()
    exemplar = write_reference("ex", {1: s3_values(1)})
    bench.write_run({"references": [exemplar]})
    fake.calls.clear()
    output = bench.validate("--sample", "3", "--seed", "7")
    assert "Wrote outputs/bench/validated/sample_3.csv" in output
    sample = Path("outputs/bench/validated/sample_3.csv")
    drawn = sample.read_bytes()
    rows = read_csv(sample)
    assert list(rows[0]) == ["State", "Site", "region", "X", "Y", "Z", "documents"]
    assert [r["Site"] for r in rows] == [site(3), site(6), site(7)]  # roster order
    assert all(r["region"] and not (r["X"] or r["Y"] or r["Z"]) for r in rows)
    assert [r["documents"] for r in rows] == [url(3), url(6), url(7)]
    bench.validate("--sample", "3", "--seed", "7")
    assert sample.read_bytes() == drawn
    bench.validate("--sample", "3", "--seed", "8")
    assert sample.read_bytes() != drawn
    assert "--fresh" in bench.validate("--sample", "3", "--fresh", code=2)
    assert "exceeds the 10 available units" in bench.validate("--sample", "20")
    assert len(read_csv("outputs/bench/validated/sample_20.csv")) == 10
    assert fake.calls == []

    labelled = {t: s3_values(t, "other" if t == 6 else "ok") for t in (3, 6, 7)}
    blind = write_reference("blind_3", labelled)
    bench.write_run({"references": [exemplar, blind]})
    output = bench.validate()
    data = summary("bench")
    scored = metrics(data, "primary", "blind_3")
    assert (scored["correct"], scored["compared"]) == (8, 9)
    assert (scored["accuracy"], scored["ci95"]["accuracy"]) == (0.8889, None)
    assert data["rows"]["out_of_scope"]["blind_3"] == 0
    assert "0.89" in output and "±" not in output


# ── S5: rows domain, geothermal-like ──

ROWS_URL = "https://rows.example/lake"
ROW_FIELDS = ("feature", "rule_kind", "value", "units", "summary", "source_verbatim")


def rows_schema() -> dict:
    text = {"type": ["string", "null"]}
    return {
        "type": "object",
        "required": ["jurisdiction", "requirements"],
        "additionalProperties": False,
        "$metadata": {
            "extraction": {"main_data_array": "requirements", "context_objects": ["jurisdiction"]},
            "identity": {
                "deduplication": {
                    "key_fields": ["feature", "rule_kind", "value", "units", "source_verbatim"],
                    "partition_fields": ["jurisdiction.state", "jurisdiction.county"],
                }
            },
            "compilation": {
                "flattening": {
                    "unit_fields": ["units"],
                    "unit_normalizations": {"ft": "feet", "ft.": "feet"},
                }
            },
        },
        "properties": {
            "jurisdiction": {
                "type": "object",
                "additionalProperties": False,
                "required": ["state", "county"],
                "properties": {"state": {"type": "string"}, "county": {"type": "string"}},
            },
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(ROW_FIELDS),
                    "properties": {f: {**text, "description": f"the {f}"} for f in ROW_FIELDS},
                },
            },
        },
    }


Q = {
    "r1": "Wells shall be set back 500 ft from any residence.",
    "r2": "The minimum lot size is 1,500 acres.",
    "r3": "Noise shall not exceed 55 dBA at the property line.",
    "r4a": "Operations may start at 7:00.",
    "r4b": "Operations shall end by 19:00.",
    "r5": "The well site shall be fenced.",
}


def row(feature, kind, value, units, summary, quote_key, quote_text=None):
    return dict(
        zip(
            ROW_FIELDS,
            (feature, kind, value, units, summary, quote_text or Q[quote_key]),
            strict=True,
        )
    )


S5_PAYLOADS = {
    "m-p": [
        row("setback", "minimum", "500", "feet", "500 ft setback from homes", "r1"),
        row("lot", "minimum", "1,500", "acres", "Lot size", "r2"),
        row("noise", "maximum", "55", "dBA", "Noise cap", "r3"),
        row("hours", "operating", "7:00", None, "Start", "r4a"),
        row("hours", "operating", "19:00", None, "End", "r4b"),
    ],
    "m-a": [
        row("setback", "minimum", "500", "ft", "Minimum 500-foot setback from residences", "r1"),
        row("lot", "minimum", "1500", "acres", "Lot size", "r2"),
        row("noise", "limit", "50", "dBA", "Noise cap", "r3", Q["r3"].rstrip(".")),
        row("hours", "operating", "7:00", None, "Start", "r4a"),
        row("hours", "operating", "19:00", None, "End", "r4b"),
        row("fencing", "required", None, None, "Fence", "r5"),
    ],
}


@pytest.fixture
def rows_domain(fake):
    config = Path("config/rows/run.yaml")
    config.parent.mkdir(parents=True)
    Path("config/rows/schema.json").write_text(
        json.dumps(rows_schema(), indent=1), encoding="utf-8"
    )
    reference = Path("config/rows/samples/H.csv")
    reference.parent.mkdir()
    reference.write_text(
        "state,county,feature,rule_kind,value,units\n"
        "CA,Lake,setback,minimum,500,ft.\n"
        "CA,Lake,lot,minimum,1500,acres\n"
        "CA,Napa,setback,minimum,1000,feet\n",
        encoding="utf-8",
    )
    curated = Path("outputs/rows/discovered/curated/lake.txt")
    curated.parent.mkdir(parents=True)
    curated.write_text(
        "Lake County, California geothermal ordinance\n\n" + "\n".join(Q.values()), encoding="utf-8"
    )
    write_index(
        "rows",
        [
            {
                "relative_path": "lake.txt",
                "source_scope": "target",
                "url": ROWS_URL,
                "target_metadata": "",
            }
        ],
        [],
    )
    for model, rows in S5_PAYLOADS.items():
        fake.payloads[(model, ROWS_URL)] = {
            "data": {"jurisdiction": {"state": "CA", "county": "Lake"}, "requirements": rows},
            "cost": 0.01,
            "input_tokens": 1000,
            "output_tokens": 100,
        }

    def write(**validation):
        run = {
            "domain": "rows",
            "models": MODELS,
            "extraction": {"model": "primary"},
            "compilation": {"output": {"default_format": "csv"}},
            "validation": {
                "models": ["a"],
                "references": ["samples/H.csv"],
                "judge": "judge",
                "key_fields": ["state", "county", "feature", "rule_kind"],
                **validation,
            },
        }
        config.write_text(yaml.safe_dump(run, sort_keys=False), encoding="utf-8")

    write(quote_field="source_verbatim")
    psweep("extract", "--config", str(config), "-q")
    psweep("compile", "--config", str(config), "-q")
    return SimpleNamespace(config=config, write=write)


def cell(domain: str, feature: str, column: str) -> list[dict]:
    rows = read_csv(Path("outputs", domain, "validated", "cells.csv"))
    return [c for c in rows if (c["feature"], c["column"]) == (feature, column)]


def test_s5_rows_domain(rows_domain, fake):
    fake.verdict = judge_by({Q["r1"], Q["r3"]}, same=frozenset({"summary"}))
    output = psweep("validate", "--config", str(rows_domain.config))
    assert "H: lacks summary; unscored for this reference" in output
    data = summary("rows")
    assert data["rows"]["match"] == {"key": 2, "anchor": 3}
    assert data["rows"]["out_of_scope"] == {"H": 1}
    (record,) = [c for c in read_csv("outputs/rows/validated/cells.csv") if c["column"] == "*"]
    assert (record["feature"], record["review_priority"]) == ("fencing", "medium")
    assert [c["status"] for c in cell("rows", "setback", "units")] == ["unanimous"]
    assert [c["status"] for c in cell("rows", "lot", "value")] == ["unanimous"]
    (r1,) = cell("rows", "setback", "summary")
    assert (r1["judge_groups"], r1["review_priority"]) == ("primary,a", "low")
    (r3,) = cell("rows", "noise", "value")
    assert (r3["status"], r3["judge_groups"]) == ("disputed", "a | primary")
    pa = data["agreement"]["overall"]["primary|a"]
    assert (pa["matched"], pa["only_b"], pa["jaccard"]) == (5, 1, 0.8333)
    h = metrics(data, "primary", "H")
    assert (h["row_tp"], h["row_fp"], h["row_fn"]) == (2, 3, 0)
    assert metrics(data, "primary", "H", "summary")["compared"] == 0

    psweep("validate", "--config", str(rows_domain.config), "--sample", "1")
    assert read_csv("outputs/rows/validated/sample_1.csv") == [
        {
            "state": "CA",
            "county": "Lake",
            "feature": "",
            "rule_kind": "",
            "value": "",
            "units": "",
            "summary": "",
            "source_verbatim": "",
        }
    ]


def test_s5b_rows_without_quote_field(rows_domain, fake):
    rows_domain.write()
    fake.verdict = judge_by(set(Q.values()))
    psweep("validate", "--config", str(rows_domain.config), code=1)  # quote_field: extract policy
    psweep("extract", "--config", str(rows_domain.config), "-q")
    psweep("compile", "--config", str(rows_domain.config), "-q")
    psweep("validate", "--config", str(rows_domain.config))
    data = summary("rows")
    pa = data["agreement"]["overall"]["primary|a"]
    assert (pa["matched"], pa["jaccard"]) == (2, 0.4)
    assert (pa["only_a"], pa["only_b"]) == (1, 2)  # r3 on both sides; r5 only in A
    assert data["rows"]["ambiguous"] == {"primary": 2, "a": 2, "H": 0}  # r4: 4 rows, 1 key


def test_rows_extraction_drops_unquoted_rows_and_reuses(rows_domain, fake):
    payload = fake.payloads[("m-p", ROWS_URL)]["data"]["requirements"]
    payload.append(row("fence", "required", None, None, "Fence", "r5", "The site is walled."))
    curated = Path("outputs/rows/discovered/curated/lake.txt")
    curated.write_text(curated.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    fake.calls.clear()
    psweep("extract", "--config", str(rows_domain.config), "-q")
    psweep("extract", "--config", str(rows_domain.config), "-q")
    assert len(fake.extraction_calls("m-p")) == 1
    (path,) = Path("outputs/rows/extracted").rglob("lake.json")
    record = json.loads(path.read_text())
    assert [r["feature"] for r in record["payload"]["requirements"]] == [
        "setback",
        "lot",
        "noise",
        "hours",
        "hours",
    ]
    assert record["quality"]["unverified_quotes"] == 1
    assert (
        'unverified quote at requirements[5].source_verbatim: "The site is walled."'
        in record["quality"]["warnings"]
    )


def test_s5c_rows_gate_out_of_scope(rows_domain, fake):
    rows_domain.write(quote_field="source_verbatim", min_accuracy=0.5)
    fake.verdict = judge_by(set(Q.values()))
    output = psweep("validate", "--config", str(rows_domain.config), code=1)
    assert "H: 1 reference row out of scope" in output
    data = summary("rows")
    h = metrics(data, "primary", "H")
    assert (h["accuracy"], h["correct"], h["compared"]) == (1.0, 4, 4)
    assert data["gate"]["passed"] is False


def test_covered_keys_match_the_tables_lookup():
    roster = Roster.build(
        [{"state": "co", "site": "  Alder   PARK "}, {"state": "CO", "site": "Birch Park"}],
        ["state", "site"],
    )
    alder, _birch = roster.targets
    covered = covered_keys({"anchors": {"a.txt": {alder}, "b.txt": set()}}, roster)
    assert tuple(map(normalize, ("CO", "Alder Park"))) in covered  # a Cluster.key display
    assert tuple(map(normalize, ("CO", "Birch Park"))) not in covered


def test_preflight_freshness(fake):
    bench = Bench(fake, 2)
    for model in ("primary", "a"):
        bench.script(model, {1: {"x": 1}, 2: {"x": 2}})
    bench.write_docs()
    bench.extract_compile()
    bench.write_run({"models": ["a"]})
    bench.validate()
    challenger = Path("outputs/bench/validated/a/bench.csv")
    compiled = challenger.read_bytes()
    challenger.write_bytes(compiled + b"\n")
    fake.calls.clear()
    bench.validate()  # a stale challenger table recompiles from its records
    assert fake.calls == [] and challenger.read_bytes() == compiled

    table = Path("outputs/bench/compiled/bench.csv")
    table.write_text(table.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    output = bench.validate(code=1)
    assert "compiled table is stale or missing; run psweep compile" in output

    bench.write_schema({"x": "x of the site, as an integer"})
    output = bench.validate(code=1)
    assert "primary extraction is stale (2 documents); run psweep extract" in output
    assert fake.calls == []


def test_removed_document_leaves_every_table(fake):
    bench = Bench(fake, 2)
    for model in ("primary", "a"):
        bench.script(model, {1: {"x": 1}, 2: {"x": 2}})
    bench.write_docs()
    bench.extract_compile()
    bench.write_run({"models": ["a"]})
    bench.validate()
    Path("outputs/bench/discovered/curated/t2.txt").unlink()
    output = bench.validate(code=1)
    assert "primary extraction is stale (1 documents); run psweep extract" in output
    bench.extract_compile()
    fake.calls.clear()
    bench.validate()
    assert fake.calls == []
    for table in ("compiled", "validated/a"):
        rows = read_csv(f"outputs/bench/{table}/bench.csv")
        assert {row["Site"]: row["X"] for row in rows} == {site(1): "1", site(2): "NA"}


def test_overlapping_runs_are_rejected(fake):
    bench = Bench(fake, 1)
    for model in ("primary", "a"):
        bench.script(model, {1: {"x": 1}})
    bench.write_docs()
    bench.extract_compile()
    bench.write_run({"models": ["a"]})
    bench.validate()
    with publication_lock(Path("outputs/bench/.validated-publication.lock")):
        assert "Publication already active" in bench.validate("--fresh", code=1)
    assert Path("outputs/bench/validated/a/extracted").is_dir()
    bench.validate()
