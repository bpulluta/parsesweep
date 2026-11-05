"""One file-type definition for discovery, extraction and the CLI."""

from __future__ import annotations

from click.testing import CliRunner

from psweep.cli.commands_extract import extract
from psweep.discovery import constants
from psweep.extraction import document_utils
from psweep.extraction.transcription import AUDIO_EXTENSIONS


def test_discovery_downloads_exactly_what_extraction_reads():
    assert constants.DOWNLOADABLE_EXTENSIONS == document_utils.SUPPORTED_EXTENSIONS
    assert constants.DOCUMENT_EXTENSIONS == document_utils.DOCUMENT_EXTENSIONS
    assert AUDIO_EXTENSIONS <= document_utils.DOCUMENT_EXTENSIONS
    assert document_utils.SUPPORTED_EXTENSIONS == (
        document_utils.DOCUMENT_EXTENSIONS | document_utils.WEB_PAGE_EXTENSIONS
    )
    assert not document_utils.DOCUMENT_EXTENSIONS & document_utils.WEB_PAGE_EXTENSIONS


def test_every_supported_type_has_a_reader():
    readable = set(document_utils._TEXT_READERS) | {".pdf"} | AUDIO_EXTENSIONS
    assert readable == document_utils.SUPPORTED_EXTENSIONS


def test_extract_help_lists_every_supported_type():
    result = CliRunner().invoke(extract, ["--help"], terminal_width=200)
    assert result.exit_code == 0
    listed = result.output.split("Supported file types:")[1].replace(",", " ")
    assert set(listed.split()) == document_utils.SUPPORTED_EXTENSIONS
