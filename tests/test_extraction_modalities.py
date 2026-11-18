"""Selected-page vision: config, rendering, routing and typed request content."""

from __future__ import annotations

import base64
import struct
from types import SimpleNamespace

import pytest

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.extraction import pdf_utils
from psweep.extraction.llm_client import LLMClient
from psweep.extraction.prepared_document import visual_pages
from psweep.extraction.request_policy import (
    RequestNotAdmittedError,
    RouteLimits,
    assess_request,
    estimate_request_tokens,
    image_block,
)

pymupdf = pytest.importorskip("pymupdf")

SCHEMA = {"type": "object", "properties": {}}


def _resolved(tmp_path, value=None):
    body = "" if value is None else f"  input_mode: {value}\n"
    path = tmp_path / "run.yaml"
    path.write_text(f"schema: s.json\nextraction:\n  skip_existing: true\n{body}", encoding="utf-8")
    return resolve_command_config(
        command="extract", cli_values={}, config_data=load_runtime_config_file(path)
    )


@pytest.mark.parametrize("mode", ["text", "auto", "vision"])
def test_input_mode_is_a_strict_enum(tmp_path, mode):
    assert _resolved(tmp_path, mode)["input_mode"] == mode


def test_input_mode_defaults_to_unset_text_only(tmp_path):
    assert _resolved(tmp_path).get("input_mode") in {None, "text"}


@pytest.mark.parametrize("bad", ["visual", "Vision", "true", "1"])
def test_input_mode_typo_fails(tmp_path, bad):
    with pytest.raises(RuntimeConfigError, match="input_mode"):
        _resolved(tmp_path, f"'{bad}'")


# ── routing ────────────────────────────────────────────────────────────


def _scope(*statuses):
    return {
        "pages": [
            {"page": n, "status": status, "chars": 0, "image_fraction": 0.0}
            for n, status in statuses
        ]
    }


def test_text_mode_never_attaches_pages():
    assert visual_pages("text", _scope((1, "unreadable"), (2, "native"))) == []


def test_auto_mode_attaches_only_unreadable_pages_with_original_numbers():
    scope = _scope((4, "native"), (7, "unreadable"), (9, "ocr"), (12, "unreadable"))
    assert visual_pages("auto", scope) == [7, 12]


def test_auto_mode_keeps_clean_documents_text_only():
    assert visual_pages("auto", _scope((1, "native"), (2, "blank"), (3, "ocr"))) == []


def test_vision_mode_attaches_meaningful_pages_not_blank_ones():
    scope = _scope((3, "native"), (5, "blank"), (8, "ocr"), (9, "unreadable"))
    assert visual_pages("vision", scope) == [3, 8, 9]


@pytest.mark.parametrize("mode", ["auto", "vision"])
def test_non_pdf_scope_has_no_pages(mode):
    assert visual_pages(mode, {}) == []


# ── rendering ──────────────────────────────────────────────────────────


def _pdf(tmp_path, pages=4):
    path = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"Rate page {n + 1}", fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def test_render_pages_returns_only_selected_pages_with_original_numbers(tmp_path):
    rendered = pdf_utils.render_pages(_pdf(tmp_path), [4, 2])
    assert [image.page for image in rendered] == [2, 4]
    assert all(image.png.startswith(b"\x89PNG") for image in rendered)
    assert all(image.width > 0 and image.height > 0 for image in rendered)


def test_render_pages_rejects_pages_beyond_the_document(tmp_path):
    with pytest.raises(ValueError, match="beyond"):
        pdf_utils.render_pages(_pdf(tmp_path, 2), [3])


# ── typed content ──────────────────────────────────────────────────────


def _png(width, height):
    """Minimal bytes with a valid PNG signature and IHDR dimensions."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _image(page=3, width=800, height=1000):
    return pdf_utils.PageImage(page, _png(width, height), width, height)


def _capture(client):
    sent = {}

    def fake(request, **_kw):
        sent.update(request)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"items": []}'),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    client._complete = fake
    return sent


def _vision_client(monkeypatch, supported=True):
    monkeypatch.setattr(
        "psweep.extraction.llm_client.litellm.supports_vision", lambda **_k: supported
    )
    return LLMClient(api_key="k", model="gpt-5.5")


def test_extract_sends_labeled_image_blocks_after_the_text(monkeypatch):
    client = _vision_client(monkeypatch)
    sent = _capture(client)
    client.extract("page text", SCHEMA, page_images=[_image(3), _image(7)])

    content = sent["messages"][-1]["content"]
    assert isinstance(content, list)
    kinds = [block["type"] for block in content]
    assert kinds == ["text", "text", "image_url", "text", "image_url"]
    assert "page text" in content[0]["text"]
    assert content[1]["text"] == "[Image of PDF page 3]"
    assert content[3]["text"] == "[Image of PDF page 7]"
    url = content[2]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == _image(3).png


def test_extract_without_images_keeps_string_content(monkeypatch):
    client = _vision_client(monkeypatch)
    sent = _capture(client)
    client.extract("page text", SCHEMA)
    assert isinstance(sent["messages"][-1]["content"], str)


def test_unsupported_vision_route_holds_before_any_request(monkeypatch):
    client = _vision_client(monkeypatch, supported=False)
    sent = _capture(client)
    with pytest.raises(RequestNotAdmittedError, match="vision"):
        client.extract("t", SCHEMA, page_images=[_image()], admit=True)
    assert sent == {}


def test_unknown_vision_support_holds(monkeypatch):
    def unknown(**_k):
        raise ValueError("unmapped")

    monkeypatch.setattr("psweep.extraction.llm_client.litellm.supports_vision", unknown)
    client = LLMClient(api_key="k", model="gpt-5.5")
    with pytest.raises(RequestNotAdmittedError, match="vision"):
        client.extract("t", SCHEMA, page_images=[_image()], admit=True)


def test_image_blocks_have_a_conservative_nonzero_estimate():
    small = estimate_request_tokens(
        [{"role": "user", "content": [image_block(_image(1, 100, 100))]}]
    )
    large = estimate_request_tokens(
        [{"role": "user", "content": [image_block(_image(1, 1600, 2000))]}]
    )
    assert 0 < small < large
    assert large >= 1600 * 2000 // 750  # at least the pixels-per-token rule


def test_unreadable_image_dimensions_are_not_guessed_small():
    bad = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    with pytest.raises(RequestNotAdmittedError):
        estimate_request_tokens([{"role": "user", "content": [bad]}])


def test_oversized_visual_request_is_held_by_known_window():
    messages = [{"role": "user", "content": [image_block(_image(1, 2000, 2000))] * 5}]
    with pytest.raises(RequestNotAdmittedError, match="context_window_exceeded"):
        assess_request(messages, None, RouteLimits(input_tokens=5000))


# ── pipeline integration ───────────────────────────────────────────────


def test_document_extractor_passes_images_to_every_generation():
    from psweep.extraction.document_extractor import DocumentExtractor

    extractor = DocumentExtractor(api_key="k", model="gpt-5.5")
    seen = []

    def fake(text, schema, **kw):
        seen.append(kw["page_images"])
        return {"data": {}, "cost": 0, "input_tokens": 1, "output_tokens": 1}

    extractor.client.extract = fake
    images = [_image(2)]
    extractor.extract("t", SCHEMA, page_images=images)
    assert seen == [images]


def test_visual_page_selection_in_scanned_pdf_end_to_end(tmp_path, monkeypatch):
    """A raster page whose OCR fails is sent as an image in auto mode."""
    from psweep import pipeline
    from psweep.extraction.prepared_document import capture_source

    path = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 160), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda _page: "")

    inputs = {"max_context": 100000}
    text_plan = {"inputs": inputs, "context": {}, "anchors": {}, "roster": None}
    with capture_source(path) as source:
        with pytest.raises(ValueError, match="no source text"):
            pipeline._prepared_input(path, text_plan, None, None, source)
        inputs["input_mode"] = "auto"
        _text, _prompt, _ranges, scope = pipeline._prepared_input(
            path, text_plan, None, None, source
        )
    assert visual_pages("auto", scope) == [1]
