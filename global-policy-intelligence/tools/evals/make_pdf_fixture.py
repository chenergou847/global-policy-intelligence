"""生成评测用 PDF fixture（文字版，含真实条款文本）。"""
import os
import zlib

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "site", "static", "rule.pdf")

TEXT = (
    "Notice on Piloting Cross-border Cash Pooling (Hu Jin Fa [2026] No. 33). "
    "Article 1. Qualified multinational companies may establish a cross-border "
    "cash pool for centralized collection, payment and netting of funds among "
    "their domestic and overseas member entities. "
    "Article 2. The single-company quota shall not exceed 5 billion RMB, and the "
    "aggregate quota of a group shall not exceed 20 billion RMB. "
    "Article 3. Banks shall verify quota compliance before processing each "
    "payment, and shall retain supporting records for at least five years. "
    "Article 4. An entity that exceeds its quota shall be required to restore "
    "compliance within ten working days and may be subject to regulatory "
    "measures including supervisory talks and orders to correct. "
    "Article 5. This notice takes effect on 1 December 2026 and remains valid "
    "until 30 November 2029."
)


def make_pdf(text: str) -> bytes:
    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1", "replace"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


os.makedirs(os.path.dirname(TARGET), exist_ok=True)
with open(TARGET, "wb") as f:
    f.write(make_pdf(TEXT))
print("wrote", TARGET, os.path.getsize(TARGET), "bytes")
