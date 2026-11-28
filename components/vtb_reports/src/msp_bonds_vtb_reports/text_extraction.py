"""Bounded in-memory text extraction for VTB XLSX/PDF documents.

Original files are read as bytes only. No OCR, temporary document copies, or
remote services are used.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_PART_BYTES = 16 * 1024 * 1024
MAX_PDF_PAGES = 300
MAX_PDF_DECLARED_STREAM_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000


@dataclass(frozen=True)
class ExtractionResult:
    detected_format: str
    status: str
    text: Optional[str]
    reason: Optional[str] = None
    pages: Optional[int] = None
    characters: Optional[int] = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_nodes(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter() if _local_name(node.tag) == "t")


def _is_date_format(code: str) -> bool:
    cleaned = re.sub(r'"[^\"]*"|\\.|\[[^\]]*\]', "", code.casefold())
    return bool(re.search(r"(?:y{1,4}|d{1,4}|h{1,2}|s{1,2})", cleaned))


def _xlsx_date(value: str, *, date_1904: bool) -> Optional[str]:
    try:
        serial = float(value)
        if not math.isfinite(serial) or serial < 0:
            return None
        whole_days = math.floor(serial)
        fraction = serial - whole_days
        if date_1904:
            epoch = datetime(1904, 1, 1)
            delta_days = serial
        else:
            # Excel's 1900 date system includes the fictitious 1900-02-29.
            epoch = datetime(1899, 12, 30)
            delta_days = serial + 1 if serial < 60 else serial
        result = epoch + timedelta(days=delta_days)
    except (OverflowError, ValueError):
        return None
    if fraction < 1e-8:
        return result.date().isoformat()
    return result.replace(microsecond=0).isoformat(sep=" ")


def _styles_date_flags(archive: ZipFile, names: set[str]) -> list[bool]:
    styles = "xl/styles.xml"
    if styles not in names:
        return []
    info = archive.getinfo(styles)
    if info.file_size > MAX_XML_PART_BYTES:
        raise ValueError("XLSX styles part exceeds the size limit")
    root = ElementTree.fromstring(archive.read(info))
    custom_formats: dict[int, str] = {}
    for item in root.iter():
        if _local_name(item.tag) == "numFmt":
            try:
                custom_formats[int(item.attrib.get("numFmtId", ""))] = item.attrib.get("formatCode", "")
            except ValueError:
                continue
    builtin_dates = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
    xfs = next((item for item in root.iter() if _local_name(item.tag) == "cellXfs"), None)
    if xfs is None:
        return []
    flags: list[bool] = []
    for xf in xfs:
        try:
            numfmt = int(xf.attrib.get("numFmtId", "0"))
        except ValueError:
            numfmt = 0
        flags.append(numfmt in builtin_dates or _is_date_format(custom_formats.get(numfmt, "")))
    return flags


def _extract_xlsx(data: bytes) -> ExtractionResult:
    try:
        with ZipFile(BytesIO(data)) as workbook:
            entries = workbook.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "XLSX entry limit exceeded")
            if sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "XLSX expanded-size limit exceeded")
            names = {item.filename for item in entries}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                return ExtractionResult("UNKNOWN", "UNSUPPORTED", None, "ZIP does not have the required XLSX structure")
            by_name = {item.filename: item for item in entries}
            if any(item.flag_bits & 0x1 for item in entries):
                return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "Encrypted XLSX entries are not supported")

            book_info = by_name["xl/workbook.xml"]
            if book_info.file_size > MAX_XML_PART_BYTES:
                return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "XLSX workbook part exceeds the size limit")
            book_root = ElementTree.fromstring(workbook.read(book_info))
            book_pr = next((item for item in book_root.iter() if _local_name(item.tag) == "workbookPr"), None)
            date_1904 = bool(book_pr is not None and book_pr.attrib.get("date1904", "0").casefold() in {"1", "true"})
            date_flags = _styles_date_flags(workbook, names)

            shared: list[str] = []
            shared_part = by_name.get("xl/sharedStrings.xml")
            if shared_part is not None:
                if shared_part.file_size > MAX_XML_PART_BYTES:
                    return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "XLSX shared strings part exceeds the size limit")
                root = ElementTree.fromstring(workbook.read(shared_part))
                shared = [_text_nodes(item) for item in root.iter() if _local_name(item.tag) == "si"]

            sheet_names = sorted(
                name for name in names
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
                and "/" not in name[len("xl/worksheets/"):]
            )
            chunks: list[str] = []
            for name in sheet_names:
                part = by_name[name]
                if part.file_size > MAX_XML_PART_BYTES:
                    return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "An XLSX worksheet exceeds the size limit")
                root = ElementTree.fromstring(workbook.read(part))
                for row in (item for item in root.iter() if _local_name(item.tag) == "row"):
                    row_values: list[str] = []
                    for cell in (item for item in row if _local_name(item.tag) == "c"):
                        kind = cell.attrib.get("t", "")
                        value = next((item.text or "" for item in cell if _local_name(item.tag) == "v"), "")
                        if kind == "s" and value:
                            try:
                                row_values.append(shared[int(value)])
                            except (ValueError, IndexError):
                                return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "Invalid shared-string reference in XLSX")
                        elif kind == "inlineStr":
                            row_values.append(_text_nodes(cell))
                        elif kind == "d" and value:
                            row_values.append(value)
                        elif kind in {"", "n", "str"} and value:
                            try:
                                style_id = int(cell.attrib.get("s", "0"))
                            except ValueError:
                                style_id = -1
                            if style_id >= 0 and style_id < len(date_flags) and date_flags[style_id]:
                                row_values.append(_xlsx_date(value, date_1904=date_1904) or value)
                            else:
                                # Numeric values stay in memory because account IDs and
                                # date serials are often stored in non-string worksheet cells.
                                row_values.append(value)
                    if row_values:
                        chunks.append(" ".join(value.strip() for value in row_values if value and value.strip()))

            text = "\n".join(value for value in chunks if value)
            return ExtractionResult("XLSX", "OK" if text else "NEEDS_REVIEW", text or None,
                                    None if text else "No worksheet text was found", characters=len(text))
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError, RuntimeError, ValueError, OverflowError):
        return ExtractionResult("XLSX", "NEEDS_REVIEW", None, "XLSX archive or XML is invalid")


def _extract_pdf(data: bytes) -> ExtractionResult:
    try:
        from pypdf import PdfReader, apply_configuration
    except ImportError:
        return ExtractionResult("PDF", "EXTRACTOR_UNAVAILABLE", None, "Install the component PDF parser in the project environment")

    try:
        with apply_configuration(maximum_declared_stream_length=MAX_PDF_DECLARED_STREAM_BYTES):
            reader = PdfReader(BytesIO(data), strict=False, root_object_recovery_limit=10_000)
            if reader.is_encrypted:
                return ExtractionResult("PDF", "NEEDS_REVIEW", None, "Encrypted PDFs are not supported", pages=len(reader.pages))
            pages = reader.pages
            if len(pages) > MAX_PDF_PAGES:
                return ExtractionResult("PDF", "NEEDS_REVIEW", None, "PDF page limit exceeded", pages=len(pages))
            chunks: list[str] = []
            total_chars = 0
            for page in pages:
                contents = page.get_contents()
                if contents is not None and len(contents.get_data()) > MAX_PDF_DECLARED_STREAM_BYTES:
                    return ExtractionResult("PDF", "NEEDS_REVIEW", None, "PDF page content exceeds the size limit", pages=len(pages))
                page_text = page.extract_text() or ""
                if not page_text.strip():
                    page_text = page.extract_text(extraction_mode="layout") or ""
                total_chars += len(page_text)
                if total_chars > MAX_EXTRACTED_CHARS:
                    return ExtractionResult("PDF", "NEEDS_REVIEW", None, "PDF extracted-text limit exceeded", pages=len(pages))
                if page_text.strip():
                    chunks.append(page_text.strip())
            text = "\n".join(chunks)
            if not text.strip():
                return ExtractionResult("PDF", "NEEDS_REVIEW", None, "PDF has no usable text layer", pages=len(pages), characters=0)
            return ExtractionResult("PDF", "OK", text, pages=len(pages), characters=len(text))
    except Exception:
        # Do not expose parser diagnostics or document data in logs or API responses.
        return ExtractionResult("PDF", "NEEDS_REVIEW", None, "PDF could not be safely parsed")


def extract_document_text(
    data: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
) -> ExtractionResult:
    """Detect from bytes, not filename/MIME; return extracted text or a review status."""
    del filename, mime_type
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > MAX_INPUT_BYTES:
        return ExtractionResult("UNKNOWN", "NEEDS_REVIEW", None, "Input exceeds the configured size limit")
    if b"%PDF-" in data[:1024]:
        return _extract_pdf(data)
    if data.startswith(b"PK"):
        return _extract_xlsx(data)
    return ExtractionResult("UNKNOWN", "UNSUPPORTED", None, "Unrecognized document bytes")
