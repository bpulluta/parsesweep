"""Failure-focused tests for discovery's small publication protocol."""

from __future__ import annotations

import csv
import json
import multiprocessing
from pathlib import Path

import pytest

from psweep.discovery import publication as pub
from psweep.discovery.engine import DiscoveryEngine


def _record(
    documents: Path, relative: str = "county/report.pdf", **extra
) -> dict:
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
        pub.csv_content(
            ["relative_path", "path", "url", "review_selected"], rows
        ),
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
    paths = pub.resolve_publication_paths(
        documents, run / "manifest.json", "example"
    )
    assert paths.domain_dir == domain
    assert paths.run_curated_dir == documents.parent / "curated"
    assert paths.run_index == run / "download_index.csv"
    flat = tmp_path / "flat"
    paths = pub.resolve_publication_paths(
        flat / "documents", flat / "manifest.json", "example"
    )
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


def test_crash_during_rollback_is_recovered_next_invocation(
    tmp_path, monkeypatch
):
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


@pytest.mark.parametrize(
    "relative", ["../escape.pdf", "/escape.pdf", "", ".review/report.pdf"]
)
def test_unsafe_selected_path_preserves_old_curation(tmp_path, relative):
    """Unsafe selected path preserves old curation."""
    documents = tmp_path / "documents"
    record = _record(documents)
    old = tmp_path / "curated"
    old.mkdir()
    (old / "prior.pdf").write_bytes(b"prior")
    record["relative_path"] = relative
    with pytest.raises(pub.PublicationError):
        DiscoveryEngine._materialize_curated(
            documents_dir=documents, download_records=[record]
        )
    assert (old / "prior.pdf").read_bytes() == b"prior"
    assert not list(tmp_path.glob(".discovery-stage-*"))


def test_missing_source_and_copy_failure_preserve_old_curation(
    tmp_path, monkeypatch
):
    """Missing source and copy failure preserve old curation."""
    documents = tmp_path / "documents"
    record = _record(documents)
    curated, _, _ = pub.materialize_curated(
        documents_dir=documents, download_records=[record]
    )
    before = (curated / record["relative_path"]).read_bytes()
    Path(record["path"]).unlink()
    with pytest.raises(pub.PublicationError, match="source missing"):
        pub.materialize_curated(
            documents_dir=documents, download_records=[record]
        )
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
        pub.materialize_curated(
            documents_dir=documents, download_records=[record]
        )
    assert (curated / record["relative_path"]).read_bytes() == before


def test_materialization_carries_text_and_full_filename_review_sidecars(
    tmp_path,
):
    """Materialization carries text and full filename review
    sidecars.
    """
    documents = tmp_path / "documents"
    record = _record(documents)
    source = Path(record["path"])
    (source.parent / ".text").mkdir()
    (source.parent / ".text/report.pdf.txt").write_text("extracted")
    (source.parent / ".text/report.pdf.meta.json").write_text("{}")
    (source.parent / ".review").mkdir()
    (source.parent / ".review/report.pdf.json").write_text(
        '{"cache_key":"identity"}'
    )
    curated, count, errors = pub.materialize_curated(
        documents_dir=documents, download_records=[record]
    )
    assert count == 1 and errors == 0
    assert (curated / "county/.text/report.pdf.txt").read_text() == "extracted"
    assert (curated / "county/.text/report.pdf.meta.json").exists()
    assert (curated / "county/.review/report.pdf.json").exists()


def test_explicit_index_provenance_is_promoted_and_fields_unioned(tmp_path):
    """Explicit index provenance is promoted and fields unioned."""
    domain, run = _corpus(tmp_path)
    explicit = tmp_path / "outside/actual-index.csv"
    _index(
        explicit, [{"relative_path": "county/new.pdf", "url": "actual-origin"}]
    )
    (domain / "download_index.csv").write_text(
        "relative_path,url,prior_only\nother/stay.pdf,old-origin,retained\n"
    )
    pub.promote_curated(
        run_curated_dir=run / "curated", domain_dir=domain, run_index=explicit
    )
    with (domain / "download_index.csv").open() as handle:
        rows = {row["relative_path"]: row for row in csv.DictReader(handle)}
    assert rows["county/new.pdf"]["url"] == "actual-origin"
    assert rows["county/new.pdf"]["path"] == str(
        domain / "curated/county/new.pdf"
    )
    assert rows["other/stay.pdf"]["prior_only"] == "retained"


def test_index_replace_failure_rolls_back_all_partitions_and_index(
    tmp_path, monkeypatch
):
    """Index replace failure rolls back all partitions and index."""
    domain, run = _corpus(tmp_path)
    before = (domain / "download_index.csv").read_bytes()
    replace = Path.replace

    def fail_index(source, destination):
        if (
            source.name.startswith("new-")
            and Path(destination) == domain / "download_index.csv"
        ):
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


def test_blocked_partition_keeps_prior_files_and_index(tmp_path):
    """Blocked partition keeps prior files and index."""
    domain, run = _corpus(tmp_path)
    before = (domain / "download_index.csv").read_bytes()
    pub.promote_curated(
        run_curated_dir=run / "curated",
        domain_dir=domain,
        blocked_partitions={Path("county")},
        attempted_partitions={Path("county")},
        run_index=run / "download_index.csv",
    )
    assert (domain / "curated/county/old.pdf").exists()
    with (domain / "download_index.csv").open() as handle:
        assert next(csv.DictReader(handle))["url"] == "old-url"
    assert before  # Header may acquire incoming fields; provenance stays old.


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


@pytest.mark.parametrize(
    "corruption", ["json", "token", "destination", "backup"]
)
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


def test_latest_pointer_atomic_and_flat_does_not_write_ancestors(
    tmp_path, monkeypatch
):
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("no symlinks")
        ),
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
