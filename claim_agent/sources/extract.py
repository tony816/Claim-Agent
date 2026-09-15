"""Text extraction for document formats used in Korean patent practice: PDF, DOCX, HWPX, HWP 5.0.

Every extractor returns the document's text in reading order (paragraphs and table cells) or raises
ExtractionError with a reason the user can act on (scanned PDF, encrypted file, missing library).
Nothing here interprets the content: the extracted text becomes a raw material exactly like a .md file,
and the original file's sha256 remains the material identity.
"""
from __future__ import annotations

import io
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

DOC_EXT = {".pdf", ".docx", ".hwpx", ".hwp"}
_MIN_CHARS_PER_PAGE = 20     # below this average a PDF is treated as scanned (no text layer)


class ExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedText:
    text: str
    format: str
    pages: int | None = None
    note: str = ""

    def as_meta(self) -> dict:
        return {"format": self.format, "pages": self.pages, "chars": len(self.text), "note": self.note}


def extract_text(path: Path) -> ExtractedText:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(path.read_bytes())
    if ext == ".docx":
        return extract_docx(path.read_bytes())
    if ext == ".hwpx":
        return extract_hwpx(path.read_bytes())
    if ext == ".hwp":
        return extract_hwp(path.read_bytes())
    raise ExtractionError(f"지원하지 않는 문서 형식: {ext}")


def read_text_any(path: Path) -> str:
    """Plain text for .md/.txt-like files, extracted text for document formats."""
    if path.suffix.lower() in DOC_EXT:
        return extract_text(path).text
    return path.read_text(encoding="utf-8-sig")


# ----------------------------------------------------------------------------- PDF
def extract_pdf(data: bytes) -> ExtractedText:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ExtractionError("PDF 읽기에는 pypdf가 필요합니다: pip install -e .") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise ExtractionError("암호화된 PDF는 열 수 없습니다. 암호를 해제한 파일을 제공하세요.") from exc
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"PDF를 읽지 못했습니다: {exc}") from exc
    text = "\n\n".join(f"[페이지 {i + 1}]\n{t}" for i, t in enumerate(pages) if t)
    if not pages or sum(len(t) for t in pages) < _MIN_CHARS_PER_PAGE * len(pages):
        raise ExtractionError("PDF에 텍스트 레이어가 거의 없습니다(스캔본으로 보임). OCR은 지원하지 않으므로 텍스트 PDF, DOCX/HWP 원본 또는 도면 이미지(PNG/JPG)로 제공하세요.")
    return ExtractedText(text, "pdf", len(pages))


# ----------------------------------------------------------------------------- DOCX
def extract_docx(data: bytes) -> ExtractedText:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("DOCX 읽기에는 python-docx가 필요합니다: pip install -e .") from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"DOCX를 읽지 못했습니다: {exc}") from exc
    lines: list[str] = []
    body = document.element.body
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for child in body.iterchildren():
        if child.tag == ns + "p":
            lines.append("".join(t.text or "" for t in child.iter(ns + "t")))
        elif child.tag == ns + "tbl":
            for row in child.iter(ns + "tr"):
                cells = []
                for cell in row.iter(ns + "tc"):
                    cells.append(" ".join("".join(t.text or "" for t in p.iter(ns + "t")) for p in cell.iter(ns + "p")).strip())
                lines.append("\t".join(cells))
            lines.append("")
    text = "\n".join(lines).strip()
    if not text:
        raise ExtractionError("DOCX에서 본문 텍스트를 찾지 못했습니다.")
    return ExtractedText(text, "docx")


# ----------------------------------------------------------------------------- HWPX (OWPML zip)
_HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


def extract_hwpx(data: bytes) -> ExtractedText:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionError("HWPX 파일이 손상되었거나 HWPX 형식이 아닙니다.") from exc
    names = sorted(n for n in zf.namelist() if n.startswith("Contents/section") and n.endswith(".xml"))
    if not names:
        raise ExtractionError("HWPX 안에 Contents/section*.xml이 없습니다.")
    lines: list[str] = []
    for name in names:
        try:
            root = ElementTree.fromstring(zf.read(name))
        except ElementTree.ParseError as exc:
            raise ExtractionError(f"HWPX 본문 XML을 읽지 못했습니다: {name}") from exc
        for para in root.iter(_HP + "p"):
            parts = [t.text or "" for t in para.iter(_HP + "t")]
            line = "".join(parts).strip()
            if line:
                lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        raise ExtractionError("HWPX에서 본문 텍스트를 찾지 못했습니다.")
    return ExtractedText(text, "hwpx", len(names))


# ----------------------------------------------------------------------------- HWP 5.0 (OLE compound)
_HWPTAG_PARA_TEXT = 0x10 + 51
_CTRL_8 = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}   # 8-wchar control codes


def hwp_records(section: bytes):
    """Yield (tag_id, level, payload) records of a decompressed HWP 5.0 section stream."""
    i, n = 0, len(section)
    while i + 4 <= n:
        header = struct.unpack_from("<I", section, i)[0]
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, header >> 20
        i += 4
        if size == 0xFFF:
            if i + 4 > n:
                break
            size = struct.unpack_from("<I", section, i)[0]
            i += 4
        yield tag, level, section[i:i + size]
        i += size


def hwp_para_text(payload: bytes) -> str:
    """Decode a HWPTAG_PARA_TEXT payload (UTF-16LE with inline/extended control sequences)."""
    out: list[str] = []
    k, n = 0, len(payload) // 2
    while k < n:
        code = struct.unpack_from("<H", payload, k * 2)[0]
        if code in _CTRL_8:
            if code == 9:
                out.append("\t")
            k += 8
            continue
        if code in (10, 13):
            out.append("\n")
        elif code in (30, 31):
            out.append(" ")
        elif code >= 32:
            out.append(chr(code))
        k += 1
    return "".join(out)


def extract_hwp(data: bytes) -> ExtractedText:
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("HWP 읽기에는 olefile이 필요합니다: pip install -e .") from exc
    if not olefile.isOleFile(io.BytesIO(data)):
        raise ExtractionError("HWP 5.0(OLE) 형식이 아닙니다. HWPX로 저장하거나 텍스트로 변환하세요.")
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        return _extract_hwp_ole(ole)
    finally:
        ole.close()


def _extract_hwp_ole(ole) -> ExtractedText:
    if not ole.exists("FileHeader"):
        raise ExtractionError("HWP FileHeader가 없습니다.")
    header = ole.openstream("FileHeader").read()
    flags = struct.unpack_from("<I", header, 36)[0] if len(header) >= 40 else 0
    compressed, encrypted, distribution = bool(flags & 1), bool(flags & 2), bool(flags & 4)
    if encrypted or distribution:
        raise ExtractionError("암호화 또는 배포용 HWP는 열 수 없습니다. 일반 저장본을 제공하세요.")
    sections = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")), key=lambda e: int("".join(ch for ch in e[1] if ch.isdigit()) or 0))
    if not sections:
        raise ExtractionError("HWP 본문(BodyText) 스트림이 없습니다.")
    lines: list[str] = []
    for entry in sections:
        raw = ole.openstream("/".join(entry)).read()
        if compressed:
            try:
                raw = zlib.decompress(raw, -15)
            except zlib.error as exc:
                raise ExtractionError(f"HWP 본문 압축을 풀지 못했습니다: {entry[1]}") from exc
        for tag, _level, payload in hwp_records(raw):
            if tag == _HWPTAG_PARA_TEXT:
                lines.append(hwp_para_text(payload).strip("\n"))
    text = "\n".join(line for line in lines if line.strip()).strip()
    if not text:
        raise ExtractionError("HWP에서 본문 텍스트를 찾지 못했습니다.")
    return ExtractedText(text, "hwp", len(sections))
