"""Offline PDF regression tests. Run with python tools/tests/test_pdf.py."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector.pdfx import extract_pdf_text, is_pdf, normalize_pdf_text


def make_pdf(pages: list[str | None]) -> bytes:
    """Use PdfWriter to create text pages or image-only pages (None)."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        resources = DictionaryObject()
        content = ContentStream(None, writer)
        if text is not None:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            })
            resources[NameObject("/Font")] = DictionaryObject({
                NameObject("/F1"): writer._add_object(font),
            })
            content.operations = [
                ([], b"BT"),
                ([NameObject("/F1"), NumberObject(12)], b"Tf"),
                ([NumberObject(72), NumberObject(720)], b"Td"),
                ([TextStringObject(text)], b"Tj"),
                ([], b"ET"),
            ]
        else:
            image = DecodedStreamObject()
            image.set_data(b"\x00\x00\x00")
            image.update({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            })
            resources[NameObject("/XObject")] = DictionaryObject({
                NameObject("/Im1"): writer._add_object(image),
            })
            content.operations = [
                ([], b"q"),
                ([NumberObject(400), NumberObject(0), NumberObject(0),
                  NumberObject(600), NumberObject(72), NumberObject(72)], b"cm"),
                ([NameObject("/Im1")], b"Do"),
                ([], b"Q"),
            ]
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(content)
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    assert len(PdfReader(io.BytesIO(data), strict=True).pages) == len(pages)
    return data


class PdfTests(unittest.TestCase):
    def test_valid_text_pdf(self) -> None:
        text = "Policy (approved) requires records and quarterly reporting. " * 4
        extracted, method = extract_pdf_text(make_pdf([text]))
        self.assertEqual(extracted.strip(), text.strip())
        self.assertEqual(method, "pypdf")

    def test_multiple_text_pages(self) -> None:
        text, method = extract_pdf_text(make_pdf(["First clause. " * 10,
                                                  "Second clause. " * 10]))
        self.assertIn("First clause.", text)
        self.assertIn("Second clause.", text)
        self.assertEqual(method, "pypdf")

    def test_corrupt_pdf_is_failed_even_with_text_or_image_markers(self) -> None:
        fake = (b"%PDF-1.4\n1 0 obj\n<< /Subtype /Image >>\nstream\n"
                b"BT (" + b"False policy text. " * 20 + b") Tj ET\n"
                b"endstream\nendobj\n%%EOF\n")
        self.assertEqual(extract_pdf_text(fake), ("", "failed"))
        self.assertEqual(extract_pdf_text(make_pdf(["Valid text. " * 20])[:200]),
                         ("", "failed"))

    def test_non_pdf_is_failed(self) -> None:
        self.assertEqual(extract_pdf_text(b"<html>not a PDF</html>"), ("", "failed"))

    def test_scanned_pdf(self) -> None:
        self.assertEqual(extract_pdf_text(make_pdf([None, None])), ("", "scanned"))

    def test_mixed_pdf_is_partial_in_either_page_order(self) -> None:
        text = "Readable policy body exceeds the full text threshold. " * 10
        for pages in ([text, None], [None, text]):
            with self.subTest(pages=pages):
                extracted, method = extract_pdf_text(make_pdf(pages))
                self.assertEqual(extracted.strip(), text.strip())
                self.assertEqual(method, "pypdf:partial")

    def test_blank_pdf_is_not_scanned(self) -> None:
        self.assertEqual(extract_pdf_text(make_pdf([""])), ("", "failed"))

    def test_threshold(self) -> None:
        for count, expected in ((1, "pypdf:partial"), (99, "pypdf:partial"),
                                (100, "pypdf"), (101, "pypdf")):
            with self.subTest(count=count):
                text, method = extract_pdf_text(make_pdf(["x" * count]))
                self.assertEqual(len(text.strip()), count)
                self.assertEqual(method, expected)

    def test_threshold_ignores_surrounding_whitespace(self) -> None:
        _, method = extract_pdf_text(make_pdf([" " * 10 + "x" * 99 + " " * 10]))
        self.assertEqual(method, "pypdf:partial")

    def test_is_pdf(self) -> None:
        self.assertTrue(is_pdf(make_pdf([""])))
        self.assertTrue(is_pdf(b"prefix\n%PDF-1.4\n"))
        self.assertFalse(is_pdf(b"plain text"))

    def test_normalize(self) -> None:
        self.assertEqual(normalize_pdf_text("０１２３４５６７８９（）〔〕［］"),
                         "0123456789()[][]")


if __name__ == "__main__":
    unittest.main()
