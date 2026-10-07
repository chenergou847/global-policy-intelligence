# -*- coding: utf-8 -*-
"""collector · PDF 正文提取（零外部依赖优先）

大量政策文件的正式文本只存在于 PDF 附件里。分层策略：

  ① 若环境装了 pypdf / pdfminer.six → 用最好的
  ② 标准库 + zlib 流解压 + 文本算子(Tj/TJ)抽取 —— 覆盖绝大多数文字版 PDF
  ③ 判断是否为扫描件（有图像、无文字层）→ 明确标记 scanned_pdf，不假装成功

第 ③ 步很重要：扫描件必须显式失败，否则后续会拿空正文当"无动态"。
"""
from __future__ import annotations

import re
import zlib

# ------------------------------------------------------------------ 第 ① 层
def _via_pypdf(data: bytes) -> str | None:
    try:
        import io

        from pypdf import PdfReader  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        chunks = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(chunks)
    except Exception:  # noqa: BLE001
        return None


def _via_pdfminer(data: bytes) -> str | None:
    try:
        import io

        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        return extract_text(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 第 ② 层
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_TEXT_OP = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj|\[(?:[^\[\]]*)\]\s*TJ")
_PAREN_STR = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _decode_pdf_string(raw: bytes) -> str:
    """PDF 字符串：处理转义 + 常见 UTF-16BE/GBK 编码。"""
    body = raw[1:-1]
    body = (
        body.replace(b"\\(", b"(")
        .replace(b"\\)", b")")
        .replace(b"\\\\", b"\\")
        .replace(b"\\n", b"\n")
        .replace(b"\\r", b"")
        .replace(b"\\t", b"\t")
    )
    if body.startswith(b"\xfe\xff"):
        try:
            return body[2:].decode("utf-16-be", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    for enc in ("utf-8", "gb18030", "utf-16-be", "latin-1"):
        try:
            text = body.decode(enc)
            if text.strip():
                return text
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _via_stdlib(data: bytes) -> str:
    """解压所有 FlateDecode 流，抽取 Tj/TJ 文本算子。

    注意：真实 PDF 的正文流几乎总是 FlateDecode 压缩的；但也存在未压缩流，
    所以解压失败时必须回退到原始字节，否则会漏抓（这是实测发现的真实缺陷）。
    """
    out: list[str] = []
    for m in _STREAM.finditer(data):
        blob = m.group(1)
        decoded: bytes | None = None
        for candidate in (blob, blob.rstrip(b"\r\n"), blob.lstrip(b"\r\n")):
            try:
                decoded = zlib.decompress(candidate)
                break
            except Exception:  # noqa: BLE001
                continue
        if decoded is None:
            decoded = blob          # 未压缩流：直接用原始字节
        if b"Tj" not in decoded and b"TJ" not in decoded:
            continue
        pieces: list[str] = []
        for op in _TEXT_OP.finditer(decoded):
            for s in _PAREN_STR.finditer(op.group(0)):
                pieces.append(_decode_pdf_string(s.group(0)))
            pieces.append(" ")
        line = "".join(pieces)
        if line.strip():
            out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------ 对外接口
_SCANNED_HINT = re.compile(rb"/Subtype\s*/Image|/XObject\s*<<[^>]*?/Image")


def is_pdf(data: bytes) -> bool:
    return bool(data[:5] == b"%PDF-") or b"%PDF-" in data[:1024]


def extract_pdf_text(data: bytes) -> tuple[str, str]:
    """返回 (正文, 方法说明)。方法为 'pypdf' / 'pdfminer' / 'stdlib' / 'scanned' / 'failed'。"""
    if not is_pdf(data):
        return "", "failed: 不是有效 PDF"

    for name, fn in (("pypdf", _via_pypdf), ("pdfminer", _via_pdfminer)):
        text = fn(data)
        if text and len(text.strip()) >= 100:
            return text, name

    text = _via_stdlib(data)
    if len(text.strip()) >= 100:
        return text, "stdlib"

    if _SCANNED_HINT.search(data):
        return text, "scanned"       # 有图像层、无文字层 → 需要 OCR/VLM
    return text, "failed"


def normalize_pdf_text(text: str) -> str:
    """政府 PDF 常用全角数字与括号，转半角便于检索与文号提取。"""
    table = str.maketrans(
        "０１２３４５６７８９（）〔〕［］", "0123456789()[]"
    )
    return text.translate(table)
