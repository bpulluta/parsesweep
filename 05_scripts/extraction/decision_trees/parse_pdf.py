"""Example on parsing an existing PDF file on-disk for ordinances."""
from functools import partial
import json

import openai
from langchain.text_splitter import RecursiveCharacterTextSplitter

from rex import init_logger
from elm.base import ApiBase
from elm.web.document import PDFDocument
from elm.utilities import validate_azure_api_params
from elm.ords.services.openai import OpenAIService
from elm.ords.utilities import RTS_SEPARATORS
from extraction.parse import StructuredOrdinanceParser
from elm.ords.services.provider import RunningAsyncServices as ARun


async def extract_ordinance_values(doc, **kwargs):
    """Extract ordinance values from a temporary vector store.

    Parameters
    ----------
    wizard : elm.wizard.EnergyWizard
        Instance of the EnergyWizard class used for RAG.
    location : str
        Name of the groundwater conservation district or county.
    **kwargs
        Keyword-value pairs used to initialize an
        `elm.ords.llm.LLMCaller` instance.

    Returns
    -------
    values : dict
        Dictionary of values extracted from the vector store.
    """

    parser = StructuredOrdinanceParser(**kwargs)
    values  = await parser.parse(doc.text)

    return values

MODEL = 'egswaterord-gpt4.1-mini'
if __name__ == '__main__':
    init_logger('elm', log_level='INFO')

    # download this from https://app.box.com/s/a8oi8jotb9vnu55rzdul7e291jnn7hmq
    fp_pdf = '11790_DC_Permit.pdf'

    fp_txt_all = fp_pdf.replace('.pdf', '_all.txt')
    fp_txt_clean = fp_pdf.replace('.pdf', '_clean.txt')
    fp_out = fp_pdf.replace('.pdf', '.json')
    

    doc = PDFDocument.from_file(fp_pdf)

    text_splitter = RecursiveCharacterTextSplitter(
        RTS_SEPARATORS,
        chunk_size=3000,
        chunk_overlap=300,
        length_function=partial(ApiBase.count_tokens, model=MODEL),
    )

    # setup LLM and Ordinance service/utility classes
    azure_api_key, azure_version, azure_endpoint = validate_azure_api_params()
    client = openai.AsyncAzureOpenAI(api_key=azure_api_key,
                                     api_version=azure_version,
                                     azure_endpoint=azure_endpoint)
    llm_service = OpenAIService(client, rate_limit=1e9)
    services = [llm_service]
    kwargs = dict(llm_service=llm_service, model=MODEL, temperature=0)

    values = ARun.run(services, extract_ordinance_values(doc, **kwargs))

    # save outputs
    with open(fp_out, 'w') as f:
        json.dump(values, f, indent=2)
