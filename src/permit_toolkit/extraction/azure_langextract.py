"""Azure OpenAI adapter for LangExtract."""

import os
import openai
from langextract.providers.openai import OpenAILanguageModel
import langextract.data as data


class AzureOpenAILanguageModel(OpenAILanguageModel):
    """Azure OpenAI adapter for LangExtract."""
    
    def __init__(self, model_id, api_key, **kwargs):
        # Copy attribute setup from parent
        self.model_id = model_id
        self.api_key = api_key
        self.organization = None
        self.format_type = kwargs.get('format_type', data.FormatType.JSON)
        self.temperature = kwargs.get('temperature', 0.0)
        self.max_workers = kwargs.get('max_workers', 10)
        self._extra_kwargs = kwargs
        
        # Azure-specific configuration
        self.azure_endpoint = kwargs.get('azure_endpoint') or os.environ.get("AZURE_OPENAI_ENDPOINT")
        self.api_version = kwargs.get('api_version') or os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
        
        if not self.azure_endpoint:
            raise ValueError("azure_endpoint must be provided or set in AZURE_OPENAI_ENDPOINT environment variable")
        
        # Create Azure client
        self._client = openai.AzureOpenAI(
            api_key=self.api_key,
            azure_endpoint=self.azure_endpoint,
            api_version=self.api_version
        )
