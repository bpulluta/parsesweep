"""Recover export publication under the output writer lock."""

import json
import shutil
import re
from pathlib import Path

from psweep.utils.file_io import atomic_json


def journal_path(output: Path) -> Path:
    """Keep the journal outside the replaced directory."""
    return output.parent / f".{output.name}-publication.json"


def prepare_export(
    output: Path, stage: Path, names: list[str], fresh: bool
) -> None:
    """Persist the rollback plan before touching output."""
    entries = (
        [{"name": name, "had_old": (output / name).exists()} for name in names]
        if not fresh
        else [{"name": "", "had_old": output.exists()}]
    )
    atomic_json(
        journal_path(output),
        {
            "version": 1,
            "state": "prepared",
            "stage": stage.name,
            "fresh": fresh,
            "entries": entries,
        },
    )


def commit_export(output: Path) -> None:
    """Mark publication complete before deleting rollback evidence."""
    path = journal_path(output)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = "committed"
    atomic_json(path, data)
    recover_export(output)


def recover_export(output: Path) -> None:
    """Roll back an interrupted publication or clean committed state."""
    journal = journal_path(output)
    if not journal.exists():
        return
    data, stage = _load_plan(output)
    if data["state"] != "committed":
        failures = []
        for index, entry in reversed(list(enumerate(data["entries"]))):
            try:
                _restore_entry(output, stage, index, entry, data["fresh"])
            except (OSError, ValueError) as exc:
                failures.append(f"{entry['name']}: {type(exc).__name__}")
        if failures:
            msg = (
                f"Export rollback incomplete. Recovery details: {stage}; "
                + "; ".join(failures)
            )
            raise ValueError(msg)
    # Persist completion before cleanup can be interrupted.
    if data["state"] != "committed":
        data["state"] = "committed"
        atomic_json(journal, data)
    journal.unlink()
    shutil.rmtree(stage)


def _load_plan(output: Path) -> tuple[dict, Path]:
    try:
        data = json.loads(journal_path(output).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = "Unreadable export journal; refusing publication"
        raise ValueError(msg) from exc
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or not isinstance(data.get("state"), str)
        or type(data.get("fresh")) is not bool
    ):
        msg = "Invalid export recovery journal; refusing publication"
        raise ValueError(msg)
    if (
        data["version"] != 1
        or data["state"] not in {"prepared", "committed"}
        or not isinstance(data.get("stage"), str)
        or not isinstance(data.get("entries"), list)
        or not data["entries"]
    ):
        msg = "Invalid export recovery journal; refusing publication"
        raise ValueError(msg)
    if not re.fullmatch(r"\.psweep-export-[a-z0-9_]{8}", data["stage"]):
        msg = "Unsafe export recovery stage name"
        raise ValueError(msg)
    stage = output.parent / data["stage"]
    if stage.is_symlink() or not stage.is_dir():
        msg = "Export recovery stage is missing or unsafe"
        raise ValueError(msg)
    entries = data["entries"]
    for entry in entries:
        _validate_entry(entry, fresh=data["fresh"], count=len(entries))
    if len({entry["name"] for entry in entries}) != len(entries):
        msg = "Duplicate export recovery entries"
        raise ValueError(msg)
    for name in ("files", "backup"):
        if (stage / name).is_symlink():
            msg = "Unsafe export recovery stage; refusing publication"
            raise ValueError(msg)
    return data, stage


def _validate_entry(entry: object, *, fresh: bool, count: int) -> None:
    if (
        not isinstance(entry, dict)
        or type(entry.get("had_old")) is not bool
        or not isinstance(entry.get("name"), str)
    ):
        msg = "Unsafe export recovery entry"
        raise ValueError(msg)
    name = entry["name"]
    if fresh:
        valid = count == 1 and not name
    else:
        valid = bool(name) and all(
            (
                Path(name).name == name,
                name not in {".", ".."},
                "\\" not in name,
            )
        )
    if not valid:
        msg = "Unsafe export recovery entry"
        raise ValueError(msg)


def _restore_entry(
    output: Path, stage: Path, index: int, entry: dict, fresh: bool
) -> None:
    name = entry["name"]
    destination = output if fresh else output / name
    staged = stage / "files" if fresh else stage / "files" / name
    backup = stage / "backup" / ("old" if fresh else f"{index}-{name}")
    if backup.exists() or backup.is_symlink():
        _remove(destination)
        backup.replace(destination)
    elif not entry["had_old"] and not staged.exists():
        _remove(destination)
    elif entry["had_old"] and not destination.exists():
        msg = "Original export and backup are both missing"
        raise ValueError(msg)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
