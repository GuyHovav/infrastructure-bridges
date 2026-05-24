"""
PDF text and image extraction utilities.

Sits between raw PDFs on disk and the LLM agents. Responsible for:
  1. Extracting clean text from each page (pdfplumber)
  2. Extracting embedded images (PyMuPDF / fitz)

Design decisions:
  - Returns plain strings — agents work on text, not PDF objects.
  - Merges all pages into one text blob with page markers so the LLM
    can reference page numbers in its output.
  - Images are saved to data/processed/images/{structure_number}/ and
    their file paths returned for the InspectionImage model.
"""
from pathlib import Path
from typing import Optional

import pdfplumber
import fitz  # PyMuPDF

from backend.config import settings


def extract_text(pdf_path: Path, max_pages: Optional[int] = None) -> str:
    """
    Extract all text from a PDF, joining pages with clear markers.

    Args:
        pdf_path:  Path to the PDF file.
        max_pages: Cap the number of pages processed (useful for testing).

    Returns:
        A single string with all extracted text. Page boundaries are marked
        with '--- PAGE N ---' so the LLM can reference specific pages.
    """
    pages_text = []
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages
        for i, page in enumerate(pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(f"--- PAGE {i} ---\n{text}")

    return "\n\n".join(pages_text)


def extract_images(pdf_path: Path, structure_number: str) -> list[dict]:
    """
    Extract all embedded images from a PDF and save them to disk.

    Bridge inspection PDFs contain photos of the bridge components,
    defects, and measurement reference points. We extract them for
    potential visual analysis or dashboard display.

    Args:
        pdf_path:         Path to the source PDF.
        structure_number: Bridge structure number (used for directory naming).

    Returns:
        List of dicts with keys: file_path, page_number, image_index.
        These map directly to the InspectionImage model fields.
    """
    out_dir = settings.images_dir / structure_number
    out_dir.mkdir(parents=True, exist_ok=True)

    extracted = []
    doc = fitz.open(str(pdf_path))

    for page_num, page in enumerate(doc, start=1):
        image_list = page.get_images(full=True)
        for img_index, img_ref in enumerate(image_list):
            xref = img_ref[0]
            try:
                base_image = doc.extract_image(xref)
                ext = base_image["ext"]          # jpg, png, etc.
                img_bytes = base_image["image"]

                filename = f"page{page_num:02d}_img{img_index:02d}.{ext}"
                dest = out_dir / filename

                with open(dest, "wb") as f:
                    f.write(img_bytes)

                extracted.append({
                    "file_path"  : str(dest),
                    "page_number": page_num,
                    "image_index": img_index,
                    "caption"    : None,           # Filled later if needed
                    "component_shown": None,
                })
            except Exception:
                pass  # Skip unreadable image xrefs

    doc.close()
    return extracted
