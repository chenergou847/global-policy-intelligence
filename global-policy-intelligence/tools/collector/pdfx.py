# -*- coding: utf-8 -*-
"""PDF text extraction via pypdf; scanned pages require OCR separately."""
from __future__ import annotations

import io

from pypdf import PdfReader


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]


def extract_pdf_text(data: bytes) -> tuple[str, str]:
    """Return (text, method): pypdf, pypdf:partial, scanned, or failed.

    Mixed text/scanned documents and text below the existing 100-character
    threshold are partial. Callers must not promote that method to full.
    """
    if not is_pdf(data):
        return "", "failed"
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        chunks: list[str] = []
        scanned_pages = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text)
            elif len(page.images):
                scanned_pages += 1
        text = "\n".join(chunks)
        if not text.strip():
            return "", "scanned" if scanned_pages else "failed"
        if scanned_pages or len(text.strip()) < 100:
            return text, "pypdf:partial"
        return text, "pypdf"
    except Exception:  # noqa: BLE001
        return "", "failed"


def normalize_pdf_text(text: str) -> str:
    """Normalize full-width digits and brackets for search and metadata."""
    table = str.maketrans(
        "０１２３４５６７８９（）〔〕［］", "0123456789()[][]"
    )
    return text.translate(table)
