"""Failure-focused tests for discovery's small publication protocol."""

from __future__ import annotations

import csv
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from psweep.discovery import publication as pub
from psweep.discovery.engine import DiscoveryEngine


def _record(documents: Path, relative: str = "county/report.pdf", **extra) -> dict:
    source = documents / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"new report")
    return {
        "status": "downloaded",
        "relative_path": relative,
        "path": str(source),
        **extra,
    }


def _index(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        pub.csv_content(["relative_path", "path", "url", "review_selected"], rows),
        encoding="utf-8",
    )


def _corpus(tmp_path: Path):
    domain = tmp_path / "domain"
    run = domain / "runs/r1"
    source = run / "curated/county"
    source.mkdir(parents=True)
    (source / "new.pdf").write_bytes(b"new")
    old = domain / "curated/county"
    old.mkdir(parents=True)
    (old / "old.pdf").write_bytes(b"old")
    _index(
        domain / "download_index.csv",
        [{"relative_path": "county/old.pdf", "url": "old-url"}],
    )
    _index(
        run / "download_index.csv",
        [{"relative_path": "county/new.pdf", "url": "new-url"}],
    )
    return domain, run


def test_resolve_default_external_documents_and_flat_locations(tmp_path):
    """Resolve default external documents and flat locations."""
    domain = tmp_path / "domain"
    run = domain / "runs/r1"
    documents = tmp_path / "external/documents"
    paths = pub.resolve_publication_paths(documents, run / "manifest.json", "example")
    assert paths.domain_dir == domain
    assert paths.run_curated_dir == documents.parent / "curated"
    assert paths.run_index == run / "download_index.csv"
    flat = tmp_path / "flat"
    paths = pub.resolve_publication_paths(flat / "documents", flat / "manifest.json", "example")
    assert paths.domain_dir == flat
    assert paths.run_curated_dir == flat / "run-curated"
    assert paths.run_index == flat / "run-download_index.csv"
    assert paths.domain_dir != tmp_path


def test_prepared_journal_before_any_swap_keeps_prior_files(tmp_path):
    """Prepared journal before any swap keeps prior files."""
    root = tmp_path / "domain"
    root.mkdir()
    old = root / "curated"
    old.mkdir()
    (old / "old.pdf").write_bytes(b"old")
    stage = pub._new_stage(root)
    new = stage / "new-0"
    new.mkdir()
    (new / "new.pdf").write_bytes(b"new")
    entries = [
        {
            "destination": str(old),
            "staged": str(new),
            "backup": str(stage / "backup-0"),
            "had_old": True,
            "state": "planned",
        }
    ]
    data = {
        "version": 1,
        "token": stage.name.removeprefix(".discovery-stage-"),
        "state": "prepared",
        "entries": entries,
        "plan_sha256": pub._plan_digest(entries),
    }
    pub.atomic_json(root / ".discovery-publication.json", data)
    with pub.discovery_publication(root):
        assert (old / "old.pdf").read_bytes() == b"old"
    assert not stage.exists()


def test_crash_during_rollback_is_recovered_next_invocation(tmp_path, monkeypatch):
    """Crash during rollback is recovered next invocation."""
    root = tmp_path / "domain"
    root.mkdir()
    stage, data = _interrupted_plan(root)
    original = Path.replace

    def fail_restore(source, destination):
        if source.name == "backup-0":
            raise OSError("interrupted rollback")
        return original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_restore)
        with (
            pytest.raises(OSError, match="interrupted rollback"),
            pub.discovery_publication(root),
        ):
            pass
    assert Path(data["entries"][0]["backup"]).exists()
    with pub.discovery_publication(root):
        assert (root / "curated/old.pdf").read_bytes() == b"old"
    assert not stage.exists()


def test_committed_journal_cleanup_keeps_successful_publication(tmp_path):
    """Committed journal cleanup keeps successful publication."""
    root = tmp_path / "domain"
    root.mkdir()
    stage, data = _interrupted_plan(root)
    data["state"] = "committed"
    pub.atomic_json(root / ".discovery-publication.json", data)
    with pub.discovery_publication(root):
        assert (root / "curated/new.pdf").read_bytes() == b"new"
        assert not (root / "curated/old.pdf").exists()
    assert not stage.exists()


@pytest.mark.parametrize("relative", ["../escape.pdf", "/escape.pdf", "", ".review/report.pdf"])
def test_unsafe_selected_path_preserves_old_curation(tmp_path, relative):
    """Unsafe selected path preserves old curation."""
    documents = tmp_path / "documents"
    record = _record(documents)
    old = tmp_path / "curated"
    old.mkdir()
    (old / "prior.pdf").write_bytes(b"prior")
    record["relative_path"] = relative
    with pytest.raises(pub.PublicationError):
        DiscoveryEngine._materialize_curated(documents_dir=documents, download_records=[record])
    assert (old / "prior.pdf").read_bytes() == b"prior"
    assert not list(tmp_path.glob(".discovery-stage-*"))


def test_missing_source_and_copy_failure_preserve_old_curation(tmp_path, monkeypatch):
    """Missing source and copy failure preserve old curation."""
    documents = tmp_path / "documents"
    record = _record(documents)
    curated, _, _ = pub.materialize_curated(documents_dir=documents, download_records=[record])
    before = (curated / record["relative_path"]).read_bytes()
    Path(record["path"]).unlink()
    with pytest.raises(pub.PublicationError, match="source missing"):
        pub.materialize_curated(documents_dir=documents, download_records=[record])
    assert (curated / record["relative_path"]).read_bytes() == before
    Path(record["path"]).write_bytes(b"replacement")
    monkeypatch.setattr(
        Path,
        "hardlink_to",
        lambda *_: (_ for _ in ()).throw(OSError("hardlink unavailable")),
    )
    monkeypatch.setattr(
        pub.shutil,
        "copy2",
        lambda *_: (_ for _ in ()).throw(OSError("copy failure")),
    )
    with pytest.raises(OSError, match="copy failure"):
        pub.materialize_curated(documents_dir=documents, download_records=[record])
    assert (curated / record["relative_path"]).read_bytes() == before


def test_materialization_carries_full_filename_text_caches(tmp_path):
    """Materialization carries full-filename text caches."""
    documents = tmp_path / "documents"
    record = _record(documents)
    source = Path(record["path"])
    (source.parent / ".text").mkdir()
    (source.parent / ".text/report.pdf.txt").write_text("extracted")
    (source.parent / ".text/report.pdf.meta.json").write_text("{}")
    curated, count, errors = pub.materialize_curated(
        documents_dir=documents, download_records=[record]
    )
    assert count == 1 and errors == 0
    assert (curated / "county/.text/report.pdf.txt").read_text() == "extracted"
    assert (curated / "county/.text/report.pdf.meta.json").exists()


def test_explicit_index_provenance_is_promoted_and_fields_unioned(tmp_path):
    """Explicit index provenance is promoted and fields unioned."""
    domain, run = _corpus(tmp_path)
    explicit = tmp_path / "outside/actual-index.csv"
    _index(explicit, [{"relative_path": "county/new.pdf", "url": "actual-origin"}])
    (domain / "download_index.csv").write_text(
        "relative_path,url,prior_only\nother/stay.pdf,old-origin,retained\n"
    )
    pub.promote_curated(run_curated_dir=run / "curated", domain_dir=domain, run_index=explicit)
    with (domain / "download_index.csv").open() as handle:
        rows = {row["relative_path"]: row for row in csv.DictReader(handle)}
    assert rows["county/new.pdf"]["url"] == "actual-origin"
    assert rows["county/new.pdf"]["path"] == str(domain / "curated/county/new.pdf")
    assert rows["other/stay.pdf"]["prior_only"] == "retained"


def test_index_replace_failure_rolls_back_all_partitions_and_index(tmp_path, monkeypatch):
    """Index replace failure rolls back all partitions and index."""
    domain, run = _corpus(tmp_path)
    before = (domain / "download_index.csv").read_bytes()
    replace = Path.replace

    def fail_index(source, destination):
        if source.name.startswith("new-") and Path(destination) == domain / "download_index.csv":
            raise OSError("index publication failed")
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_index)
    with pytest.raises(OSError, match="index publication failed"):
        pub.promote_curated(
            run_curated_dir=run / "curated",
            domain_dir=domain,
            run_index=run / "download_index.csv",
        )
    assert (domain / "curated/county/old.pdf").read_bytes() == b"old"
    assert not (domain / "curated/county/new.pdf").exists()
    assert (domain / "download_index.csv").read_bytes() == before
    assert not (domain / ".discovery-publication.json").exists()


def test_missing_provenance_does_not_swap_files(tmp_path):
    """Missing provenance does not swap files."""
    domain, run = _corpus(tmp_path)
    _index(run / "download_index.csv", [])
    with pytest.raises(pub.PublicationError, match="Missing provenance"):
        pub.promote_curated(
            run_curated_dir=run / "curated",
            domain_dir=domain,
            run_index=run / "download_index.csv",
        )
    assert (domain / "curated/county/old.pdf").exists()


def test_blocked_partition_publishes_additively(tmp_path):
    """An incomplete partition adds its files but never replaces or withdraws."""
    domain, run = _corpus(tmp_path)
    pub.promote_curated(
        run_curated_dir=run / "curated",
        domain_dir=domain,
        blocked_partitions={Path("county")},
        attempted_partitions={Path("county")},
        run_index=run / "download_index.csv",
    )
    assert (domain / "curated/county/old.pdf").read_bytes() == b"old"
    assert (domain / "curated/county/new.pdf").read_bytes() == b"new"
    with (domain / "download_index.csv").open() as handle:
        assert [row["url"] for row in csv.DictReader(handle)] == ["old-url", "new-url"]


@pytest.mark.parametrize("fresh", [True, False])
def test_republished_identical_file_keeps_its_text_cache(tmp_path, fresh):
    """OCR/transcript text survives republication of the same bytes only."""
    from psweep.extraction.document_utils import read_text_cache, write_text_cache

    domain, run = tmp_path / "domain", tmp_path / "domain/runs/r1"
    old, new = domain / "curated/county", run / "curated/county"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    for name, before, after in (("same.mp3", b"audio", b"audio"), ("changed.pdf", b"v1", b"v2")):
        (old / name).write_bytes(before)
        write_text_cache(old / name, f"text of {name}", method="transcript", page_count=4)
        (new / name).write_bytes(after)
        os.utime(new / name, ns=(1, 1))
    rows = [{"relative_path": f"county/{n}", "url": n} for n in ("same.mp3", "changed.pdf")]
    _index(domain / "download_index.csv", rows)
    _index(run / "download_index.csv", rows)
    pub.promote_curated(
        run_curated_dir=run / "curated",
        domain_dir=domain,
        attempted_partitions={Path("county")} if fresh else None,
        run_index=run / "download_index.csv",
    )
    text, meta = read_text_cache(old / "same.mp3", return_meta=True)
    assert (text, meta["method"], meta["page_count"]) == ("text of same.mp3", "transcript", 4)
    assert read_text_cache(old / "changed.pdf") is None


def _interrupted_plan(domain: Path):
    staging = pub._new_stage(domain)
    old = domain / "curated"
    old.mkdir()
    (old / "old.pdf").write_bytes(b"old")
    new = staging / "new-0"
    new.mkdir()
    (new / "new.pdf").write_bytes(b"new")
    backup = staging / "backup-0"
    data = {
        "version": 1,
        "token": staging.name.removeprefix(".discovery-stage-"),
        "state": "prepared",
        "entries": [
            {
                "destination": str(old),
                "staged": str(new),
                "backup": str(backup),
                "had_old": True,
                "state": "planned",
            }
        ],
    }
    data["plan_sha256"] = pub._plan_digest(data["entries"])
    pub.atomic_json(domain / ".discovery-publication.json", data)
    old.replace(backup)
    new.replace(old)
    return staging, data


def test_next_invocation_recovers_interrupted_swap_and_dead_owner(tmp_path):
    """Next invocation recovers interrupted swap and dead owner."""
    domain = tmp_path / "domain"
    domain.mkdir()
    stage, _ = _interrupted_plan(domain)
    (domain / ".discovery-publication.lock").write_text(
        json.dumps({"pid": 999999999, "token": "old"})
    )
    with pub.discovery_publication(domain):
        assert (domain / "curated/old.pdf").read_bytes() == b"old"
        assert not (domain / "curated/new.pdf").exists()
    assert not stage.exists()
    assert not (domain / ".discovery-publication.json").exists()


@pytest.mark.parametrize("corruption", ["json", "token", "destination", "backup"])
def test_corrupt_recovery_plan_fails_without_overwriting(tmp_path, corruption):
    """Corrupt recovery plan fails without overwriting."""
    domain = tmp_path / "domain"
    domain.mkdir()
    _, data = _interrupted_plan(domain)
    journal = domain / ".discovery-publication.json"
    if corruption == "json":
        journal.write_text("{")
    elif corruption == "token":
        data["token"] = str(Path("..") / ".." / "escape")
        pub.atomic_json(journal, data)
    elif corruption == "destination":
        data["entries"][0]["destination"] = str(tmp_path)
        pub.atomic_json(journal, data)
    else:
        pub._remove(Path(data["entries"][0]["backup"]))
    with (
        pytest.raises(pub.PublicationError),
        pub.discovery_publication(domain),
    ):
        pytest.fail("corrupt journal must reject writer")
    assert (domain / "curated/new.pdf").read_bytes() == b"new"
    assert journal.exists()


def _hold_lock(root: str, connection):
    with pub.discovery_publication(Path(root)):
        connection.send("locked")
        connection.recv()
    connection.close()


@pytest.mark.integration
@pytest.mark.parametrize("abrupt", [False, True])
def test_separate_process_lock_rejects_active_and_releases_after_exit(
    tmp_path,
    abrupt,
):
    """Separate process lock rejects active and releases after exit."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_lock, args=(str(tmp_path), child))
    process.start()
    try:
        assert parent.poll(20) and parent.recv() == "locked"
        with (
            pytest.raises(pub.PublicationError, match="already active"),
            pub.discovery_publication(tmp_path),
        ):
            pytest.fail("concurrent publication")
        if abrupt:
            process.terminate()
        else:
            parent.send("done")
        process.join(20)
        assert process.exitcode is not None
        if not abrupt:
            assert process.exitcode == 0
        with pub.discovery_publication(tmp_path):
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()


def test_latest_pointer_atomic_and_flat_does_not_write_ancestors(tmp_path, monkeypatch):
    """Latest pointer atomic and flat does not write ancestors."""
    flat = tmp_path / "flat"
    flat.mkdir()
    pub.refresh_latest_pointer(flat, domain_dir=flat)
    assert (flat / "latest").resolve() == flat
    assert not (tmp_path / "latest").exists()
    (flat / "latest").unlink()
    monkeypatch.setattr(
        pub.os,
        "symlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no symlinks")),
    )
    pub.refresh_latest_pointer(flat, domain_dir=flat)
    assert (flat / "latest.txt").read_text().strip() == str(flat)


def test_atomic_json_failure_keeps_prior_bytes(tmp_path, monkeypatch):
    """Atomic json failure keeps prior bytes."""
    path = tmp_path / "manifest.json"
    pub.atomic_json(path, {"old": True})
    before = path.read_bytes()
    monkeypatch.setattr(
        Path,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("replace failure")),
    )
    with pytest.raises(OSError):
        pub.atomic_json(path, {"new": True})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".manifest.json-*"))


@pytest.mark.parametrize("blocked", [False, True])
def test_merged_index_keeps_one_path_per_owner(tmp_path, blocked):
    """Renamed and unselected owners lose their old path whatever its name,
    in blocked partitions too (a final decision); only a rename needs a promotion.
    """
    index = tmp_path / "download_index.csv"
    index.write_text(
        "relative_path,target_id,url\n"
        "p/old-slug.pdf,t,https://a.gov/doc\n"
        "p/held.pdf,t,https://a.gov/rejected\n"
        "p/other.pdf,u,https://b.gov/other\n"
    )
    content, removed = pub._merged_index(
        run_index=None,
        domain_index=index,
        promoted_paths=set() if blocked else {"p/new-slug.pdf"},
        replaced_partitions=set(),
        withdrawn={("t", "https://a.gov/rejected")},
        is_blocked=lambda _part: blocked,
        run_content="relative_path,target_id,url\np/new-slug.pdf,t,https://a.gov/doc\n",
    )
    paths = {row.split(",")[0] for row in content.splitlines()[1:]}
    if blocked:
        assert removed == {"p/held.pdf"} and paths == {"p/old-slug.pdf", "p/other.pdf"}
    else:
        assert removed == {"p/old-slug.pdf", "p/held.pdf"}
        assert paths == {"p/new-slug.pdf", "p/other.pdf"}


def test_query_variants_of_one_shared_page_get_distinct_stable_names(tmp_path):
    """Pagination pages share a basename; each keeps its own published name."""
    listing = "https://site.org/where-is-it-available"
    urls = [listing, *(f"{listing}?p_page={n}" for n in (2, 3, 4))]

    def records(order):
        return [
            {
                "status": "downloaded",
                "relative_path": "_shared/where-is-it-available.html",
                "url": urls[n],
                "source_url": listing,
            }
            for n in order
        ]

    first = records([0, 1, 2, 3])
    index = tmp_path / "index.csv"
    pub.assign_document_names(first, index, set())
    names = {r["url"]: r["relative_path"] for r in first}
    assert len(set(names.values())) == len(urls)
    assert names[listing] == "_shared/where-is-it-available.html"
    index.write_text(
        pub.csv_content(["relative_path", "url", "source_url"], first),
        encoding="utf-8",
    )
    rerun = records([3, 1, 0, 2])
    pub.assign_document_names(rerun, index, set())
    assert {r["url"]: r["relative_path"] for r in rerun} == names


def _ocr_pdf(path: Path, content: bytes) -> Path:
    from psweep.extraction.pdf_utils import OcrPageCache

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    import hashlib

    OcrPageCache(
        path.parent / ".text" / f"{path.name}.ocr", hashlib.sha256(content).hexdigest()
    ).put(1, "raw ocr")
    return path


def _entry_names(pdf: Path) -> list[str]:
    return sorted(p.name for p in (pdf.parent / ".text" / f"{pdf.name}.ocr").glob("*.json"))


def test_promotion_removal_swaps_remove_page_entries_with_the_document(tmp_path):
    """A renamed owner's old consolidated document takes its OCR entries along."""
    domain, run = tmp_path / "domain", tmp_path / "domain/runs/r1"
    old = _ocr_pdf(domain / "curated/county/old.pdf", b"old")
    (run / "curated/county").mkdir(parents=True)
    (run / "curated/county/new.pdf").write_bytes(b"new")
    _index(domain / "download_index.csv", [{"relative_path": "county/old.pdf", "url": "u"}])
    _index(run / "download_index.csv", [{"relative_path": "county/new.pdf", "url": "u"}])
    assert _entry_names(old) == ["1.json"]
    pub.promote_curated(
        run_curated_dir=run / "curated", domain_dir=domain, run_index=run / "download_index.csv"
    )
    assert not old.exists()
    assert not list((domain / "curated/county/.text").glob("old.pdf.ocr/*"))


@pytest.mark.parametrize("same_bytes", [True, False])
def test_inherit_carries_page_entries_only_to_identical_bytes(tmp_path, same_bytes):
    """A same-SHA re-download inherits OCR pages; a different file does not."""
    old = _ocr_pdf(tmp_path / "old/report.pdf", b"v1")
    new = tmp_path / "new/report.pdf"
    new.parent.mkdir()
    new.write_bytes(b"v1" if same_bytes else b"v2")
    pub.inherit_text_caches([new], [old])
    assert _entry_names(new) == (["1.json"] if same_bytes else [])


def test_review_ledger_leaves_llm_selected_blank_when_no_review_ran(tmp_path):
    """A downloaded status is not an LLM verdict; only a real review fills the column."""
    import csv

    from psweep.discovery import DiscoveryEngine

    def record(name, **review):
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4")
        return {
            "path": str(path),
            "relative_path": f"_shared/{name}",
            "status": "downloaded",
            "target_label": "t",
            **review,
        }

    def rows(records):
        run_dir = tmp_path / "run"
        run_dir.mkdir(exist_ok=True)
        review = DiscoveryEngine._write_review_index(download_records=records, run_dir=run_dir)
        with review.open(newline="") as handle:
            return {r["relative_path"]: r for r in csv.DictReader(handle)}

    unreviewed = rows([record("a.pdf")])
    assert unreviewed["_shared/a.pdf"]["llm_selected"] == ""
    assert unreviewed["_shared/a.pdf"]["curated_selected"] == "True"

    mixed = rows(
        [
            record("a.pdf", review_selected=True),
            record("b.pdf", review_selected=False),
            record("c.pdf"),
        ]
    )
    assert mixed["_shared/a.pdf"]["llm_selected"] == "True"
    assert mixed["_shared/b.pdf"]["llm_selected"] == "False"


_SHARED_URLS = {
    name: f"https://site.gov/{name}" for name in ("keep", "reject", "drop", "failed", "old")
}


def _blocked_corpus(tmp_path: Path):
    """A consolidated `_shared` partition holding five documents of one source."""
    domain = tmp_path / "domain"
    rows = []
    for name, url in _SHARED_URLS.items():
        document = domain / f"curated/_shared/{name}.pdf"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_bytes(name.encode())
        rows.append(
            {"relative_path": f"_shared/{name}.pdf", "url": url, "source_url": "https://site.gov"}
        )
    (domain / "download_index.csv").write_text(
        pub.csv_content(["relative_path", "url", "source_url"], rows), encoding="utf-8"
    )
    return domain


def _decide(tmp_path: Path, domain: Path, decisions: dict[str, dict]):
    """Publish a run in which each named document carries its own record fields."""
    from psweep.discovery.publication import publish_manual_curation, resolve_publication_paths

    run = domain / "runs/r2"
    documents = run / "documents"
    records = []
    for name, fields in decisions.items():
        source = documents / f"_shared/{name}.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(name.encode())
        records.append(
            {
                "status": "downloaded",
                "relative_path": f"_shared/{name}.pdf",
                "path": str(source),
                "url": _SHARED_URLS.get(name, f"https://site.gov/{name}"),
                "source_url": "https://site.gov",
                **fields,
            }
        )
    paths = resolve_publication_paths(
        documents, run / "manifest.json", "example", domain_dir=domain
    )
    content = pub.csv_content(
        ["relative_path", "url", "source_url"],
        [r for r in records if pub.record_is_curated(r, review_ran=True)],
    )
    publish_manual_curation(
        paths=paths,
        download_records=records,
        run_index=paths.run_index,
        run_index_content=content,
        metadata_updates={},
        blocked_partitions={Path("_shared")},
        attempted_partitions={Path("_shared")},
    )
    with (domain / "download_index.csv").open() as handle:
        indexed = {Path(row["relative_path"]).stem for row in csv.DictReader(handle)}
    return {p.stem for p in (domain / "curated/_shared").glob("*.pdf")}, indexed


_KEEP = {"review_selected": True}


def test_final_decisions_withdraw_consolidated_documents_in_a_blocked_partition(tmp_path):
    """A human reject or a final non-selection applies even though another
    document in the partition failed; a retryable failure and a document this
    run never saw stay.
    """
    domain = _blocked_corpus(tmp_path)
    files, indexed = _decide(
        tmp_path,
        domain,
        {
            "keep": _KEEP,
            "reject": {**_KEEP, "human_decision": "reject"},
            "drop": {"review_selected": False},
            "failed": {"review_failed": True, "review_retryable": True},
        },
    )
    assert files == indexed == {"keep", "failed", "old"}


def test_blocked_partition_does_not_infer_absence_for_completed_targets(tmp_path):
    """Only a decision about a document applies; 'completed means gone' does not."""
    from psweep.discovery.publication import publish_manual_curation, resolve_publication_paths

    domain = _blocked_corpus(tmp_path)
    run = domain / "runs/r2"
    paths = resolve_publication_paths(
        run / "documents", run / "manifest.json", "example", domain_dir=domain
    )
    publish_manual_curation(
        paths=paths,
        download_records=[],
        run_index=paths.run_index,
        run_index_content="relative_path,url,source_url\n",
        metadata_updates={},
        blocked_partitions={Path("_shared")},
        completed={pub.owner_key("", "https://site.gov")},
    )
    assert {p.stem for p in (domain / "curated/_shared").glob("*.pdf")} == set(_SHARED_URLS)


def test_blocked_partition_promotion_removes_the_old_path_of_a_renamed_owner(tmp_path):
    """The discover path: one URL published under a new name leaves one document."""
    domain, run = tmp_path / "domain", tmp_path / "domain/runs/r1"
    old = domain / "curated/_shared/old-slug.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    other = domain / "curated/_shared/other.pdf"
    other.write_bytes(b"other")
    _index(
        domain / "download_index.csv",
        [
            {"relative_path": "_shared/old-slug.pdf", "url": "u"},
            {"relative_path": "_shared/other.pdf", "url": "o"},
        ],
    )
    (run / "curated/_shared").mkdir(parents=True)
    (run / "curated/_shared/new-slug.pdf").write_bytes(b"new")
    _index(run / "download_index.csv", [{"relative_path": "_shared/new-slug.pdf", "url": "u"}])
    pub.promote_curated(
        run_curated_dir=run / "curated",
        domain_dir=domain,
        blocked_partitions={Path("_shared")},
        run_index=run / "download_index.csv",
    )
    assert sorted(p.name for p in (domain / "curated/_shared").glob("*.pdf")) == [
        "new-slug.pdf",
        "other.pdf",
    ]
    with (domain / "download_index.csv").open() as handle:
        assert {row["relative_path"] for row in csv.DictReader(handle)} == {
            "_shared/new-slug.pdf",
            "_shared/other.pdf",
        }
