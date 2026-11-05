# Source Patterns

Every source becomes a document and extraction reads its text, so the same
pipeline serves search results, exports, listings, code hosts and recordings.
Pick the pattern that matches where your data lives; keys are defined in the
[Configuration Reference](config-reference.md).

## 1. Search → direct document

The document is one search away (a tariff PDF, an ordinance).

```yaml
discovery:
  targets: targets.csv
  queries:
    - '"{utility_name}" {state} electric tariff filetype:pdf'
  search:
    params: {udm: web}          # Google "Web" tab: honours quotes and site:
```

## 2. Structured export (CSV / XLSX)

A site offers a "Download CSV". List the export in `sources`; it is saved under
`_shared/` and every record becomes a self-describing `Header: value | …` line,
so roster extraction sees only the rows that name each target.

```yaml
discovery:
  sources: [https://example.org/permits/export.csv]
```

Exports can be stale: use them alongside the live pages, and remember that a
target missing from an export is *not stated*, never "No".

## 3. Paginated listing

The data sits inline on numbered pages. Either list the pages in `sources`, or
crawl the "next" links:

```yaml
discovery:
  sources: [https://example.org/directory]
  crawl:
    max_depth: 10                         # longest page chain
    include_link_text_patterns: ['^next$']
    max_pages: 200                        # one budget for the whole run
```

Every HTML candidate (search results included) is crawled, and `max_pages` is
shared by the run: size it above the number of HTML candidates plus listing
pages. A listing cut short by `max_pages` resumes on the next run.

## 4. Index page → per-item documents

A table links to one document per item (permits, filings).

```yaml
discovery:
  sources: [https://agency.example.gov/permits]
  crawl:
    max_depth: 1
    include_url_patterns: ['showpublisheddocument']
    browser_escalation: true              # recover 401/403/429/451 pages in a browser
```

Capped or partial listings catch up across runs; new children of a listing that
already completed are picked up by `discover --fresh`. Sites behind a bot wall
may need `provider: selenium` with `browser_downloads: true` (see
[Security](security.md) for browser requirements).

## 5. Code hosts

- **Municode** pages are JavaScript shells; `crawl.code_host_adapters: true`
  fetches their text through Municode's API without a browser.
- **eCode360** pages are server-rendered and need nothing special.

## 6. Public database exports

Socrata, ArcGIS REST, GeoJSON and CKAN endpoints that return CSV, XLSX or JSON
record arrays go in `sources` like any export (pattern 2); JSON record arrays
are rendered one `Header: value` line per record.

## 7. Public-meeting audio

Recordings (`.mp3 .m4a .wav .ogg .flac`) are documents. Find them with a
listing crawl (pattern 4), then name a transcription model:

```yaml
models:
  primary: gpt-4o
  transcribe: gpt-4o-mini-transcribe    # any transcription model your endpoint serves
extraction:
  model: primary
  transcription_model: transcribe
```

ffmpeg comes with the pixi environment (run ParseSweep through `pixi run`).
Each recording is transcoded to mono 16 kHz audio and cut into ~5-minute chunks
at the quietest moment; chunks are transcribed in parallel and every chunk
starts with an exact `[hh:mm:ss]` offset, so quotes point to a time. Measured
cost is about **$0.22 per audio hour**. The transcript is cached beside the
recording in `.text/<filename>.txt` (under `outputs/<domain>/discovered/curated/…`
for discovered recordings), so each file is transcribed once. Document review
never pays to transcribe. On a first run, recordings have no transcript yet and
are kept without review. When a later discovery run finds the same recording
again (same SHA-256), its copy inherits the curated transcript before review,
so the recording is graded like any other document.

Streamed downloads stop at 1 GiB; set `PSWEEP_MAX_RESPONSE_BYTES` for longer
recordings (see [Document types and limits](config-reference.md#document-types-and-limits)).
