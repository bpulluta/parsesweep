from pathlib import Path

from psweep.extraction.document_utils import extract_text_from_document, is_supported_document


def test_extract_text_from_html_document(tmp_path: Path):
    html_path = tmp_path / "filing.html"
    html_path.write_text(
        """
<html>
  <head>
    <title>Apple Filing Summary</title>
    <style>.hidden { display:none; }</style>
  </head>
  <body>
    <h1>Apple Q1 2026</h1>
    <p>Net sales were $143.8 billion.</p>
    <script>console.log('ignore me')</script>
    <p>Net income was $42.1 billion.</p>
  </body>
</html>
""",
        encoding="utf-8",
    )

    extracted = extract_text_from_document(html_path)

    assert is_supported_document(html_path) is True
    assert "Apple Filing Summary" in extracted
    assert "Apple Q1 2026" in extracted
    assert "Net sales were $143.8 billion." in extracted
    assert "Net income was $42.1 billion." in extracted
    assert "ignore me" not in extracted