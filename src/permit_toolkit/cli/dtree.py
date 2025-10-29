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
import pymupdf4llm
from dotenv import load_dotenv
from rex import init_logger

from elm.web.document import PDFDocument
from elm.utilities import validate_azure_api_params
from elm.ords.services.openai import OpenAIService
from elm.ords.services.cpu import PDFLoader
from elm.ords.services.provider import RunningAsyncServices
from elm.web.file_loader import AsyncLocalFileLoader
from elm.utilities.parse import read_pdf  # , read_pdf_ocr

from permit_toolkit.extraction.decision_trees.parse import (
    StructuredOrdinanceParser,
)


logger = logging.getLogger(__name__)


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
    load_dotenv()

    init_logger("elm", log_level="DEBUG" if verbose else "INFO")

    output = Path(output)
    input_dir = Path(input_dir)
    output.mkdir(parents=True, exist_ok=True)

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

    # setup LLM and Ordinance service/utility classes
    azure_api_key, azure_version, azure_endpoint = validate_azure_api_params()
    client = openai.AsyncAzureOpenAI(
        api_key=azure_api_key,
        api_version=azure_version,
        azure_endpoint=azure_endpoint,
    )
    llm_service = OpenAIService(client, rate_limit=3e5)
    services = [llm_service, PDFLoader(max_workers=4)]
    process_sem = asyncio.Semaphore(num_docs)

    async with RunningAsyncServices(services):
        tasks = [
            asyncio.create_task(
                _process_one(fp, output_dir, model, llm_service, process_sem),
                # name=self.jurisdiction.full_name,
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

    start_time = time.monotonic()
    async with process_sem:
        # docs = await load_local_docs([fp], **file_loader_kwargs)
        # doc = docs[0]

        text = await PDFLoader.call(_read_pdf_file_pymupdf4llm, fp)

        parser = StructuredOrdinanceParser(
            llm_service=llm_service, model=model
        )
        values = await parser.parse(text)
        # values = await parser.parse(doc.text)

    output_data = {
        "source_file": fp.name,
        "extraction_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "state": None,
        "model": model,
        "qa_qc_enabled": False,
        "cost_usd": None,
        "processing_time_sec": time.monotonic() - start_time,
        "completeness_score": None,
        # "generator_count": sum(gen_set.get('numGenerators', 0) or 0 for gen_set in generator_sets),
        # "permit_number": presult.data.get('permitDetails', {}).get('permitNumber', 'N/A')
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
    return pymupdf4llm.to_markdown(pdf_fp)
