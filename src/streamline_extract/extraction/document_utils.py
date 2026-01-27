"""Universal document text extraction for multiple file formats."""

import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Supported file extensions
SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.txt', '.xlsx', '.csv', '.doc'}


def is_supported_document(file_path: Path) -> bool:
    """
    Check if a file is a supported document format.
    
    Args:
        file_path: Path to the file
        
    Returns:
        True if file format is supported
    """
    return file_path.suffix.lower() in SUPPORTED_EXTENSIONS


def extract_text_from_document(file_path: Path, page_range: Optional[tuple] = None) -> str:
    """
    Extract text from any supported document format.
    
    Supports: PDF, DOCX, TXT, XLSX, CSV, DOC
    
    Args:
        file_path: Path to the document
        page_range: Optional tuple (start_page, end_page) for PDF files only (1-indexed)
        
    Returns:
        Extracted text content
        
    Raises:
        ValueError: If file format is not supported
        RuntimeError: If extraction fails
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    ext = file_path.suffix.lower()
    
    if ext == '.pdf':
        return _extract_from_pdf(file_path, page_range)
    elif ext in {'.docx', '.doc'}:
        return _extract_from_docx(file_path)
    elif ext == '.txt':
        return _extract_from_txt(file_path)
    elif ext == '.xlsx':
        return _extract_from_xlsx(file_path)
    elif ext == '.csv':
        return _extract_from_csv(file_path)
    else:
        raise ValueError(
            f"Unsupported file format: {ext}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )


def _extract_from_pdf(pdf_path: Path, page_range: Optional[tuple] = None) -> str:
    """Extract text from PDF using existing pdf_utils."""
    from .pdf_utils import extract_text_from_pdf
    return extract_text_from_pdf(pdf_path, page_range=page_range)


def _extract_from_docx(docx_path: Path) -> str:
    """
    Extract text from DOCX file.
    
    Args:
        docx_path: Path to DOCX file
        
    Returns:
        Extracted text
    """
    try:
        import docx
    except ImportError:
        raise RuntimeError(
            "python-docx not installed. Install with: pip install python-docx"
        )
    
    try:
        doc = docx.Document(str(docx_path))
        text_parts = []
        
        # Extract text from paragraphs
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)
        
        # Extract text from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    if cell.text.strip():
                        row_text.append(cell.text.strip())
                if row_text:
                    text_parts.append(" | ".join(row_text))
        
        text = "\n".join(text_parts)
        logger.info(f"✓ Extracted {len(text):,} characters from DOCX: {docx_path.name}")
        return text
        
    except Exception as e:
        raise RuntimeError(f"Failed to extract from DOCX {docx_path.name}: {e}")


def _extract_from_txt(txt_path: Path) -> str:
    """
    Extract text from TXT file.
    
    Args:
        txt_path: Path to TXT file
        
    Returns:
        File contents
    """
    try:
        # Try UTF-8 first, fall back to other encodings
        encodings = ['utf-8', 'latin-1', 'cp1252']
        
        for encoding in encodings:
            try:
                with open(txt_path, 'r', encoding=encoding) as f:
                    text = f.read()
                logger.info(f"✓ Extracted {len(text):,} characters from TXT: {txt_path.name}")
                return text
            except UnicodeDecodeError:
                continue
        
        raise RuntimeError(f"Could not decode text file with any supported encoding")
        
    except Exception as e:
        raise RuntimeError(f"Failed to extract from TXT {txt_path.name}: {e}")


def _extract_from_xlsx(xlsx_path: Path) -> str:
    """
    Extract text from Excel XLSX file.
    
    Args:
        xlsx_path: Path to XLSX file
        
    Returns:
        Formatted text with sheet names and cell values
    """
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError(
            "openpyxl not installed. Install with: pip install openpyxl"
        )
    
    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        text_parts = []
        
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            text_parts.append(f"\n### Sheet: {sheet_name} ###\n")
            
            # Extract all rows
            for row in sheet.iter_rows(values_only=True):
                row_values = [str(cell) if cell is not None else "" for cell in row]
                # Skip completely empty rows
                if any(v.strip() for v in row_values):
                    text_parts.append(" | ".join(row_values))
        
        text = "\n".join(text_parts)
        logger.info(f"✓ Extracted {len(text):,} characters from XLSX: {xlsx_path.name}")
        return text
        
    except Exception as e:
        raise RuntimeError(f"Failed to extract from XLSX {xlsx_path.name}: {e}")


def _extract_from_csv(csv_path: Path) -> str:
    """
    Extract text from CSV file.
    
    Args:
        csv_path: Path to CSV file
        
    Returns:
        Formatted text with CSV contents
    """
    try:
        import csv
        
        text_parts = []
        
        # Try different encodings
        encodings = ['utf-8', 'latin-1', 'cp1252']
        
        for encoding in encodings:
            try:
                with open(csv_path, 'r', encoding=encoding, newline='') as f:
                    reader = csv.reader(f)
                    for row in reader:
                        # Skip empty rows
                        if any(cell.strip() for cell in row):
                            text_parts.append(" | ".join(row))
                break
            except UnicodeDecodeError:
                continue
        
        if not text_parts:
            raise RuntimeError("Could not decode CSV file with any supported encoding")
        
        text = "\n".join(text_parts)
        logger.info(f"✓ Extracted {len(text):,} characters from CSV: {csv_path.name}")
        return text
        
    except Exception as e:
        raise RuntimeError(f"Failed to extract from CSV {csv_path.name}: {e}")
