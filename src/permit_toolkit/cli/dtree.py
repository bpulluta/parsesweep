"""Command line interface for decision tree extraction"""

import json
import time
import logging
import click
import asyncio
import pprint
import multiprocessing
from pathlib import Path

import openai
from dotenv import load_dotenv
from rex import init_logger
# import pymupdf4llm

from elm.web.document import PDFDocument
from elm.utilities import validate_azure_api_params
from elm.ords.services.openai import OpenAIService, usage_from_response
from elm.ords.services.cpu import PDFLoader
from elm.ords.services.provider import RunningAsyncServices
from elm.ords.services.usage import UsageTracker
from elm.web.file_loader import AsyncLocalFileLoader
from elm.utilities.parse import read_pdf  # , read_pdf_ocr

from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.extraction.decision_trees.parse import (
    StructuredOrdinanceParser,
)


logger = logging.getLogger(__name__)
LLM_COST_REGISTRY = {
    "o1": {"prompt": 15, "response": 60},
    "o3-mini": {"prompt": 1.1, "response": 4.4},
    "gpt-4.5": {"prompt": 75, "response": 150},
    "gpt-4o": {"prompt": 2.5, "response": 10},
    "gpt-4o-mini": {"prompt": 0.15, "response": 0.6},
    "gpt-4.1": {"prompt": 2, "response": 8},
    "gpt-4.1-mini": {"prompt": 0.4, "response": 1.6},
    "gpt-4.1-nano": {"prompt": 0.1, "response": 0.4},
    "gpt-5": {"prompt": 1.25, "response": 10},
    "gpt-5-mini": {"prompt": 0.25, "response": 2},
    "gpt-5-nano": {"prompt": 0.05, "response": 0.4},
    "gpt-5-chat-latest": {"prompt": 1.25, "response": 10},
    "compassop-gpt-4o": {"prompt": 2.5, "response": 10},
    "compassop-gpt-4o-mini": {"prompt": 0.15, "response": 0.6},
    "compassop-gpt-4.1": {"prompt": 2, "response": 8},
    "compassop-gpt-4.1-mini": {"prompt": 0.4, "response": 1.6},
    "compassop-gpt-4.1-nano": {"prompt": 0.1, "response": 0.4},
    "compassop-gpt-5": {"prompt": 1.25, "response": 10},
    "compassop-gpt-5-mini": {"prompt": 0.25, "response": 2},
    "compassop-gpt-5-nano": {"prompt": 0.05, "response": 0.4},
    "compassop-gpt-5-chat-latest": {"prompt": 1.25, "response": 10},
}


@click.command()
@click.argument("input_dir", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), help="Output directory")
@click.option(
    "--model",
    default="compassop-gpt-5",
    show_default=True,
    help="Model to use",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Option to use verbose (DEBUG) logging",
)
def dtree_extract(input_dir, output, model, verbose):
    """Extract backup generator info using decision trees"""
    load_dotenv()

    output = Path(output)
    input_dir = Path(input_dir)
    output.mkdir(parents=True, exist_ok=True)

    elm_logger = init_logger("elm", log_level="DEBUG" if verbose else "INFO")
    init_logger("permit_toolkit", log_level="DEBUG" if verbose else "INFO")

    if verbose:
        today = time.strftime("%Y-%m-%d_%H_%M_%S")
        handler = logging.FileHandler(
            output / f"run_{today}.log", encoding="utf-8"
        )
        fmt = logging.Formatter(
            fmt="[%(asctime)s] %(levelname)s - %(taskName)s: %(message)s",
        )
        handler.setFormatter(fmt)
        handler.setLevel("DEBUG")
        elm_logger.addHandler(handler)

    # Need to set start method to "spawn" instead of "fork" for unix
    # systems. If this call is not present, software hangs when process
    # pool executor is launched.
    # More info here: https://stackoverflow.com/a/63897175/20650649
    multiprocessing.set_start_method("spawn")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_process_all(input_dir, output, model))


async def _process_all(input_dir, output_dir, model, num_docs=20):
    files = list(input_dir.glob("*.pdf"))

    logger.info("Processing %d PDF file(s) from %s", len(files), input_dir)

    azure_api_key, azure_version, azure_endpoint = validate_azure_api_params()
    client = openai.AsyncAzureOpenAI(
        api_key=azure_api_key,
        api_version=azure_version,
        azure_endpoint=azure_endpoint,
    )
    llm_service = OpenAIService(client, rate_limit=200_000)
    services = [llm_service, PDFLoader(max_workers=4)]
    process_sem = asyncio.Semaphore(num_docs)

    async with RunningAsyncServices(services):
        tasks = [
            asyncio.create_task(
                _process_one(fp, output_dir, model, llm_service, process_sem),
                name=fp.name,
            )
            for fp in files
        ]
        await asyncio.gather(*tasks)


async def _process_one(fp, output_dir, model, llm_service, process_sem):
    fp_out = output_dir / fp.name.replace(".pdf", ".json")

    if fp_out.exists():
        logger.info("Output file %s exists, skipping.", fp_out)
        return

    # file_loader_kwargs = {
    #     "pdf_read_coroutine": read_pdf_file,
    # }

    # if self.web_search_params.pytesseract_exe_fp is not None:
    #     _setup_pytesseract(self.web_search_params.pytesseract_exe_fp)
    #     file_loader_kwargs.update(
    #         {"pdf_ocr_read_coroutine": read_pdf_file_ocr}
    #     )

    usage_tracker = UsageTracker("totals", usage_from_response)

    start_time = time.monotonic()
    async with process_sem:
        # docs = await load_local_docs([fp], **file_loader_kwargs)
        # doc = docs[0]

        text = await PDFLoader.call(_read_pdf_file_pymupdf4llm, fp)

        parser = StructuredOrdinanceParser(
            llm_service=llm_service, usage_tracker=usage_tracker, model=model
        )
        values = await parser.parse(text)
        # values = await parser.parse(doc.text)

    permit_details = values.get("permitDetails", {})
    generator_sets = values.get("generatorSets", [])
    output_data = {
        "source_file": fp.name,
        "extraction_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "state": permit_details.get("facilityState"),
        "model": model,
        "qa_qc_enabled": False,
        "cost_usd": _compute_total_cost_from_usage(usage_tracker, model),
        "processing_time_sec": time.monotonic() - start_time,
        "completeness_score": None,
        "generator_count": sum(
            gen_set.get("numGenerators", 0) or 0 for gen_set in generator_sets
        ),
        "permit_number": permit_details.get("permitNumber", "N/A"),
        "data": values,
        "validation_notes": None,
    }

    with fp_out.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)


async def load_local_docs(fps, **kwargs):
    """Load a document for each input filepath

    Parameters
    ----------
    fps : iterable of path-like
        Iterable of paths representing documents to load.
    kwargs
        Keyword-argument pairs to initialize
        :class:`elm.web.file_loader.AsyncLocalFileLoader`.

    Returns
    -------
    list
        List of non-empty document instances containing information from
        the local documents. If a file could not be loaded (i.e.
        document instance is empty), it will not be included in the
        output list.
    """
    logger.trace("Loading docs for the following paths:\n%r", fps)
    logger.trace(
        "kwargs for AsyncLocalFileLoader:\n%s",
        pprint.PrettyPrinter().pformat(kwargs),
    )
    file_loader = AsyncLocalFileLoader(**kwargs)
    docs = await file_loader.fetch_all(*fps)

    page_lens = {
        doc.attrs.get("source_fp", "Unknown"): len(doc.pages) for doc in docs
    }
    logger.debug(
        "Loaded the following number of pages for docs:\n%s",
        pprint.PrettyPrinter().pformat(page_lens),
    )
    return [doc for doc in docs if not doc.empty]


async def read_pdf_file(pdf_fp, **kwargs):
    """Read local PDF file in a Process Pool

    Parameters
    ----------
    pdf_fp : path-like
        Path to PDF file (non-OCR).
    **kwargs
        Keyword-value arguments to pass to
        :class:`elm.web.document.PDFDocument` initializer.

    Returns
    -------
    elm.web.document.PDFDocument
        PDFDocument instances with pages loaded as text.
    """
    return await PDFLoader.call(_read_pdf_file, pdf_fp, **kwargs)


def _read_pdf_file(pdf_fp, **kwargs):
    """Utility func so that pdftotext.PDF doesn't have to be pickled"""
    with Path(pdf_fp).open("rb") as fh:
        pdf_bytes = fh.read()

    pages = read_pdf(pdf_bytes, verbose=False)
    return PDFDocument(pages, **kwargs), None


def _read_pdf_file_pymupdf4llm(pdf_fp):
    """Utility func so that pdftotext.PDF doesn't have to be pickled"""
    return extract_text_from_pdf(pdf_fp)


def _compute_total_cost_from_usage(usage_tracker, model):
    """Compute total cost from total tracked usage"""
    total_usage = usage_tracker.totals
    model_costs = LLM_COST_REGISTRY.get(model, {})
    total_cost = (
        total_usage.get("prompt_tokens", 0)
        / 1e6
        * model_costs.get("prompt", 0)
    )
    total_cost += (
        total_usage.get("response_tokens", 0)
        / 1e6
        * model_costs.get("response", 0)
    )
    return total_cost
