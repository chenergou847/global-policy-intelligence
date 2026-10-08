# -*- coding: utf-8 -*-
"""collector · 离线测试（标准库实现，无需 pytest）

    python tools/tests/run_tests.py

覆盖点全部是「会导致幻觉或漏抓」的真实失败模式：
  · 列表页解析：link_pattern 是否挡住导航链接、短标题是否被过滤
  · 正文抽取：GB2312 页面、选择器命中、JS 骨架、Cloudflare 验证页
  · 元数据：文号、生效日期、发布机关（报告里的可核验事实全靠这些）
  · 四态判定：full / listing_only / blocked / scanned_pdf
  · 去重：同一条政策跨站转载只保留一条，且保留更完整的那条
  · 幻觉校验：编造链接、改写引文、编造文号必须被拦住
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import traceback

# Windows 控制台默认 GBK，中文断言名会乱码；强制 UTF-8 输出
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
SKILL = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)

FIX = os.path.join(HERE, "fixtures")
TMP_ROOT = os.path.join(HERE, ".tmp")
SRC_YAML = os.path.join(TOOLS, "sources.yaml")


@contextlib.contextmanager
def workdir():
    """在工作区内建临时目录。

    注意：不要用 tempfile.mkdtemp —— 在 Windows 沙箱下它创建的目录会继承
    受限权限，之后无法再往里建子目录或写文件（实测 PermissionError）。
    这里改用 os.makedirs 建普通目录，与 collector 真实写证据库的路径行为一致。
    """
    os.makedirs(TMP_ROOT, exist_ok=True)
    counter = 0
    while True:
        path = os.path.join(TMP_ROOT, f"case_{os.getpid()}_{counter}")
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            break
        counter += 1
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

from collector import extract as ex            # noqa: E402
from collector import listparse as lp          # noqa: E402
from collector import pdfx                     # noqa: E402
from collector import store                    # noqa: E402
from collector import verify as vf             # noqa: E402
from collector.models import Article, FetchStatus  # noqa: E402
from collector.registry import Source          # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def read(path: str) -> str:
    with open(os.path.join(FIX, path), encoding="utf-8") as f:
        return f.read()


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))


def test(name: str):
    def deco(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILED.append((name, traceback.format_exc(limit=3)))
        return fn

    return deco


# ------------------------------------------------------------------ 列表解析
@test("T01 JSON 列表解析：字段映射 + 日期归一")
def t01() -> None:
    import json

    payload = json.loads(read("nfra_list.json"))
    src = Source(
        key="nfra", name="金融监管总局·监管动态", type="json_api",
        extract=__import__("collector.registry", fromlist=["ExtractSpec"]).ExtractSpec(
            records_path="data.rows",
            title_field="docTitle",
            date_field="publishDate",
            summary_field="docSummary",
            link_template="https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId={docId}&itemId=915",
        ),
    )
    items = lp.parse_json_records(payload, src.extract, src, "https://example.gov.cn/api")
    check("T01 条数=3", len(items) == 3, f"got {len(items)}")
    check("T01 标题", "监管评级办法" in items[0].title, items[0].title)
    check("T01 日期归一", items[0].date == "2026-09-15", str(items[0].date))
    check("T01 链接模板", "docId=5133407" in items[0].url, items[0].url)
    check("T01 初始状态=listing_only",
          items[0].fetch_status == FetchStatus.LISTING_ONLY.value, items[0].fetch_status)


@test("T02 HTML 列表解析：link_pattern 挡掉导航与短标题")
def t02() -> None:
    from collector.registry import ExtractSpec

    spec = ExtractSpec(
        link_pattern=r"/gwk/policy/\d{8}/[0-9a-f]+\.html",
        date_from_url=r"/(\d{4})(\d{2})(\d{2})/",
    )
    src = Source(key="sh", name="上海市政府", type="html_list")
    items = lp.parse_html_listing(read("listing_gov.html"), spec, src,
                                  "https://www.shanghai.gov.cn/gwk/policy/index.html")
    titles = [i.title for i in items]
    check("T02 只留 3 条（滤掉 short/导航）", len(items) == 3, f"got {len(items)}: {titles}")
    check("T02 导航链接被挡", all("联系我们" not in t for t in titles), str(titles))
    check("T02 URL 绝对化", items[0].url.startswith("https://www.shanghai.gov.cn/"), items[0].url)
    check("T02 日期来自 URL", items[0].date == "2026-09-15", str(items[0].date))


@test("T03 Feed 解析：pubDate 归一 + 短标题过滤")
def t03() -> None:
    src = Source(key="artemis", name="Artemis", type="rss", region="境外")
    items = lp.parse_feed(read("feed.xml"), src, "https://www.artemis.bm/feed/")
    check("T03 条数=2", len(items) == 2, f"got {len(items)}")
    check("T03 日期", items[0].date == "2026-09-15", str(items[0].date))
    check("T03 摘要去标签", "<p>" not in items[0].summary, items[0].summary[:60])


@test("T04 sitemap 解析：按 lastmod 过滤 + 排序")
def t04() -> None:
    src = Source(key="iais", name="IAIS", type="sitemap", region="境外")
    src.extract.link_pattern = r"iaisweb\.org/20\d{2}/\d{2}/[a-z0-9-]+"
    items = lp.parse_sitemap(read("sitemap.xml"), src, "https://www.iaisweb.org/sitemap_index.xml",
                             since="2026-06-01")
    check("T04 条数=2（contact 页被引号模式挡掉）", len(items) == 2, f"got {len(items)}")
    check("T04 倒序", items[0].date >= items[1].date, str([i.date for i in items]))


# ------------------------------------------------------------------ 正文抽取
@test("T05 正文抽取：选择器命中 + 元数据")
def t05() -> None:
    html = read("detail_gov.html")
    tree = ex.parse_html(html)
    assert tree is not None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, ["div.detail_content"])
    check("T05 命中声明选择器", how == "div.detail_content", how)
    check("T05 正文含关键条款", "500亿元" in text, text[:80])
    check("T05 标题", "国际再保险中心" in ex.extract_title(tree), ex.extract_title(tree))
    check("T05 文号提取", ex.extract_docno(text) == "沪府办发〔2026〕18号", ex.extract_docno(text))
    check("T05 生效日期", ex.parse_effective_date(text) == "2026-10-01",
          str(ex.parse_effective_date(text)))
    check("T05 发布机关", "上海市人民政府办公厅" in ex.extract_issuer(text), ex.extract_issuer(text))


@test("T06 无选择器时走启发式，仍能取到正文而非导航")
def t06() -> None:
    tree = ex.parse_html(read("detail_gov.html"))
    assert tree is not None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, [])
    check("T06 取到正文", "国际再保险中心" in text or "500亿元" in text, f"{how}: {text[:80]}")
    check("T06 未把导航当正文", "导航 首页" not in text[:50], text[:50])


@test("T07 JS 骨架页 → blocked（不假装成功）")
def t07() -> None:
    html = read("detail_js_only.html")
    tree = ex.parse_html(html)
    assert tree is not None
    text, _ = ex.extract_body(tree, [])
    status, reason = ex.classify_status(html, text, min_chars=200)
    check("T07 blocked", status == FetchStatus.BLOCKED.value, f"{status} / {reason}")


@test("T08 Cloudflare 验证页 → blocked")
def t08() -> None:
    html = read("detail_cloudflare.html")
    status, reason = ex.classify_status(html, ex.normalize_text(html), min_chars=200)
    check("T08 blocked", status == FetchStatus.BLOCKED.value, f"{status} / {reason}")
    check("T08 原因可读", "阻塞特征" in reason, reason)


@test("T09 短正文 → listing_only，且带字数说明")
def t09() -> None:
    html = "<html><body><div class='detail_content'>略</div></body></html>"
    tree = ex.parse_html(html)
    assert tree is not None
    text, _ = ex.extract_body(tree, ["div.detail_content"], min_chars=10)
    status, reason = ex.classify_status(html, text, min_chars=200)
    check("T09 listing_only", status == FetchStatus.LISTING_ONLY.value, status)
    check("T09 说明含字数", "低于阈值" in reason, reason)


# ------------------------------------------------------------------ 编码
@test("T10 GB2312 编码解码（政府站常见）")
def t10() -> None:
    import httpx

    body = "<html><body><p>跨境再保险业务资金结算便利化</p></body></html>".encode("gb2312")
    resp = httpx.Response(200, content=body, headers={"content-type": "text/html; charset=gb2312"})
    from collector.net import decode_body

    out = decode_body(resp)
    check("T10 正确解码", "跨境再保险业务" in out, out[:60])


@test("T11 元数据长串被清理，不污染正文与引文")
def t11() -> None:
    dirty = "正文开始 iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8 正文结束"
    clean = ex.normalize_text(dirty)
    check("T11 长串被移除", "iVBORw0KGgo" not in clean, clean)
    check("T11 正文保留", "正文开始" in clean and "正文结束" in clean, clean)


# ------------------------------------------------------------------ PDF
def _make_text_pdf(text: str) -> bytes:
    """构造一个最小可用、带正确 xref 且正文流为 FlateDecode 的 PDF（贴近真实站点）。"""
    import zlib

    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1", errors="replace"))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


@test("T12 PDF 文字提取（内置标准库解析器）")
def t12() -> None:
    long_text = (
        "Reinsurance collateral requirements are amended by this regulation. "
        "Insurers must hold collateral equal to 100 percent of liabilities. "
        "This rule takes effect on 1 January 2027 for all licensed reinsurers."
    )
    data = _make_text_pdf(long_text)
    check("T12 识别为 PDF", pdfx.is_pdf(data), "is_pdf false")
    text, method = pdfx.extract_pdf_text(data)
    check("T12 提取到文字", "collateral" in text.lower(), f"method={method} text={text[:80]!r}")
    check("T12 方法可追溯", method in ("stdlib", "pypdf", "pdfminer"), method)


@test("T13 扫描件（无文字层）→ scanned_pdf，不假装成功")
def t13() -> None:
    fake = b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image /Width 100 >>\nendobj\n%%EOF\n"
    text, method = pdfx.extract_pdf_text(fake)
    check("T13 判定 scanned", method == "scanned", f"method={method}")
    check("T13 正文为空", not text.strip(), repr(text[:40]))


@test("T14 非 PDF 输入 → failed，不抛异常")
def t14() -> None:
    text, method = pdfx.extract_pdf_text(b"<html>not a pdf</html>")
    check("T14 failed", method.startswith("failed"), method)
    check("T14 空正文", text == "", repr(text))


# ------------------------------------------------------------------ 去重
@test("T15 内容级去重：跨站转载只留一条，且保留更完整的")
def t15() -> None:
    body = "上海市推进国际再保险中心建设行动方案。到2028年，跨境再保险业务规模力争达到500亿元。"
    a = Article(url="https://a.example/policy/1", source="A站", title="上海推进国际再保险中心建设行动方案",
                date="2026-09-15", text=body, fetch_status=FetchStatus.LISTING_ONLY.value,
                evidence_layer="L2")
    b = Article(url="https://b.example/news/2", source="B站", title="上海推进国际再保险中心建设行动方案",
                date="2026-09-15", text=body + "补充：支持银行办理结售汇业务。" * 3,
                fetch_status=FetchStatus.FULL.value, evidence_layer="L1")
    kept, dupes = store.dedupe([a, b])
    check("T15 只留一条", len(kept) == 1, f"got {len(kept)}")
    check("T15 保留更完整的一条", kept[0].url == b.url, kept[0].url)
    check("T15 重复被记录", len(dupes) == 1 and dupes[0]["dropped_url"] == a.url, str(dupes))


@test("T16 URL 去重 + 标题指纹（同月同题）")
def t16() -> None:
    a = Article(url="https://x.example/1", source="X", title="关于跨境数据流动便利化的若干措施", date="2026-09-10")
    b = Article(url="https://x.example/1", source="X", title="关于跨境数据流动便利化的若干措施", date="2026-09-10")
    c = Article(url="https://y.example/9", source="Y", title="关于跨境数据流动便利化的若干措施！", date="2026-09-11")
    kept, dupes = store.dedupe([a, b, c])
    check("T16 三条归一为一条", len(kept) == 1, f"got {len(kept)}")
    check("T16 重复记录=2", len(dupes) == 2, str(len(dupes)))


# ------------------------------------------------------------------ 幻觉校验
def _build_evidence(root: str) -> None:
    art = Article(
        url="https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=5133407",
        source="金融监管总局·监管动态", source_key="nfra", region="境内",
        title="国家金融监督管理总局关于印发保险公司监管评级办法的通知",
        date="2026-09-15", docno="金规〔2026〕3号",
        text=(
            "国家金融监督管理总局关于印发保险公司监管评级办法的通知\n金规〔2026〕3号\n"
            "各金融监管局，各保险集团（控股）公司、保险公司：\n"
            "为加强保险公司监管评级管理，健全分类监管机制，现将《保险公司监管评级办法》印发给你们。\n"
            "监管评级每年开展一次，评级结果分为1至5级和S级。\n"
            "保险公司应当于每年4月30日前完成自评估并报送相关材料。\n"
            "本办法自2026年11月1日起施行。"
        ),
        fetch_status=FetchStatus.FULL.value, fetch_method="api", first_seen="2026-09-20",
    )
    snap = store.snapshot_article(art, root)
    index = store.load_index(root)
    index.setdefault("entries", {})[art.url] = store.index_entry(art, snap)
    store.save_index(root, index)


@test("T17 幻觉校验：合格报告通过")
def t17() -> None:
    with workdir() as root:
        _build_evidence(root)
        report = (
            "# 政策分析\n\n"
            "- 发布机关：国家金融监督管理总局\n"
            "- 文号：金规〔2026〕3号\n"
            "- 来源：https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=5133407\n\n"
            "> 本办法自2026年11月1日起施行。\n\n"
            "compliance 影响：需在 2026-11-01 前完成自评估流程改造。\n"
        )
        result = vf.verify_report(report, root)
        check("T17 通过", result.ok, str([f.to_dict() for f in result.errors]))
        check("T17 引文已核验", result.stats.get("quotes_verified", 0) >= 1, str(result.stats))


@test("T18 幻觉校验：编造链接被拦截")
def t18() -> None:
    with workdir() as root:
        _build_evidence(root)
        report = (
            "# 报告\n\n- 来源：https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=9999999\n"
            "> 本办法自2026年11月1日起施行。\n"
        )
        result = vf.verify_report(report, root)
        checks = {f.check for f in result.errors}
        check("T18 未通过", not result.ok, "应报错")
        check("T18 命中 C1", "C1-引用链接" in checks, str(checks))


@test("T19 幻觉校验：改写式引文被拦截")
def t19() -> None:
    with workdir() as root:
        _build_evidence(root)
        report = (
            "> 本办法自2027年1月1日起施行，并适用于全部保险机构及其境外分支机构。\n"
            "- 来源：https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=5133407\n"
        )
        result = vf.verify_report(report, root)
        checks = {f.check for f in result.errors}
        check("T19 命中 C2", "C2-逐字引文" in checks, str(checks))


@test("T20 幻觉校验：编造文号与金额被拦截")
def t20() -> None:
    with workdir() as root:
        _build_evidence(root)
        report = (
            "# 报告\n\n- 依据文号：金规〔2027〕99号\n"
            "- 补贴上限：单个机构不超过 3000 万元\n"
            "- 来源：https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=5133407\n"
        )
        result = vf.verify_report(report, root)
        details = " ".join(f.detail for f in result.errors)
        check("T20 命中编造文号", "金规〔2027〕99号" in details, details)
        check("T20 命中编造金额", "3000" in details, details)


@test("T21 幻觉校验：无 full 证据时不得出高置信结论")
def t21() -> None:
    with workdir() as root:
        art = Article(url="https://x.example/blocked", source="X", title="某政策",
                      fetch_status=FetchStatus.BLOCKED.value, notes="Cloudflare 拦截")
        snap = store.snapshot_article(art, root)
        index = store.load_index(root)
        index.setdefault("entries", {})[art.url] = store.index_entry(art, snap)
        store.save_index(root, index)
        result = vf.verify_report("# 报告\n\n结论：高置信度利空。\n", root)
        checks = {f.check for f in result.errors}
        check("T21 命中 C4", "C4-证据强度" in checks, str(checks))
        check("T21 未通过", not result.ok, "应报错")


# ------------------------------------------------------------------ 台账
@test("T22 证据台账 19 字段齐全")
def t22() -> None:
    art = Article(url="https://x.example/1", source="X", title="T", date="2026-09-15",
                  text="正文内容" * 60, fetch_status=FetchStatus.FULL.value, issuer="某部",
                  docno="某发〔2026〕1号", first_seen="2026-09-20")
    row = art.to_ledger_row()
    required = {
        "source", "layer", "issuer", "title", "docno", "pub_date", "effective_date",
        "policy_status", "fetch_status", "fetch_method", "text_chars", "snapshot_id",
        "head_fingerprint", "tail_fingerprint", "url", "attempts",
    }
    missing = required - set(row)
    check("T22 字段齐全", not missing, f"缺 {missing}")
    check("T22 snapshot_id 稳定", art.snapshot_id == art.snapshot_id, art.snapshot_id)
    check("T22 首尾指纹非空", bool(row["head_fingerprint"]) and bool(row["tail_fingerprint"]),
          str(row["head_fingerprint"])[:40])


@test("T24 回归：详情页返回 PDF 时不得把 PDF 二进制当正文（实测污染过证据库）")
def t24() -> None:
    from collector.fetch import _tier_html
    from collector.net import Fetcher, NetConfig

    class FakeFetcher(Fetcher):
        def __init__(self) -> None:  # 不建真实连接
            self.config = NetConfig()
            self.attempts = []
            self.transport_notes = []
            self._doc_cache = {}
            self._client = None  # type: ignore[assignment]
            self._direct_client = None

        def get_document(self, url, *, tier="html", encoding=None):  # type: ignore[override]
            pdf = _make_text_pdf("Reinsurance rules apply from 1 January 2027 in the United Kingdom.")
            return "pdf", pdf.decode("latin-1", errors="replace")

    src = Source(key="uk", name="UK legislation", type="rss")
    art = Article(url="https://www.legislation.gov.uk/uksi/2026/1", source="UK", title="SI 2026/1")
    outcome = _tier_html(src, art, FakeFetcher())  # type: ignore[arg-type]
    check("T24 不得判为 full", outcome is not None and outcome.status != FetchStatus.FULL.value,
          str(outcome.status if outcome else None))
    check("T24 不得含 PDF 二进制头", outcome is not None and "%PDF-" not in outcome.text,
          (outcome.text[:60] if outcome else ""))
    check("T24 明确标注原因", outcome is not None and "PDF" in (outcome.reason or ""),
          str(outcome.reason if outcome else ""))


@test("T25 回归：PDF 内容走 pdf 级解析后成为真实正文")
def t25() -> None:
    from collector import pdfx as _pdfx

    pdf = _make_text_pdf(
        "Reinsurance collateral requirements are amended. Insurers must hold collateral "
        "equal to 100 percent of liabilities. This rule takes effect on 1 January 2027."
    )
    text, method = _pdfx.extract_pdf_text(pdf)
    check("T25 提取成功", "collateral" in text.lower(), f"{method}: {text[:80]!r}")
    check("T25 不含 PDF 语法", "%PDF" not in text and "endstream" not in text, text[:80])


@test("T26 回归：Wayback 工具栏页不得被判为正文（实测污染过证据库）")
def t26() -> None:
    toolbar = "\n".join([
        "2 captures", "02 Oct 2026 - 03 Oct 2026", "Sep", "OCT", "Nov", "03",
        "2025", "2026", "2027", "success", "fail", "About this capture",
        "TIMESTAMPS", "The Wayback Machine - http://web.archive.org/web/2026/x.pdf",
        "Save Page Now",
    ])
    check("T26 识别为导航/工具页", ex.looks_like_nav_page(toolbar, 200), toolbar[:60])
    status, reason = ex.classify_status("", toolbar, min_chars=200)
    check("T26 判为 listing_only", status == FetchStatus.LISTING_ONLY.value, f"{status}/{reason}")

    real = (
        "第一条 为规范保险公司监管评级工作，制定本办法。\n"
        "第二条 监管评级每年开展一次，评级结果分为1至5级和S级。\n"
        "第三条 保险公司应当于每年4月30日前完成自评估并报送材料。\n"
        "第四条 本办法自2026年11月1日起施行，由中国银行保险监督管理委员会负责解释。\n"
    )
    check("T26 真实政策正文不被误判", not ex.looks_like_nav_page(real, 60), real[:40])
    status2, _ = ex.classify_status("", real, min_chars=60)
    check("T26 真实正文判为 full", status2 == FetchStatus.FULL.value, status2)


@test("T27 回归：html 级走真实 Fetcher 也能取到正文（曾因 lxml+meta charset 抛 TypeError 全线静默失败）")
def t27() -> None:
    """这是最重要的一个回归测试。

    原缺陷：lxml.fromstring 收到含 <meta charset> 的 unicode 字符串会抛
    TypeError，而 html 级把它当异常吞掉 → **所有**静态 HTML 源都变成 blocked，
    报告看起来"这个来源没有动态"。单元测试只测了片段，所以漏掉了它。
    这里用本地静态服务器 + 真实 Fetcher 端到端跑一遍。
    """
    import functools
    import http.server
    import socketserver
    import threading

    from collector.fetch import fetch_article
    from collector.net import Fetcher, NetConfig
    from collector.registry import ExtractSpec, Source

    class _Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D102
            pass

    site = os.path.join(FIX, "..", "fixtures_site")
    os.makedirs(site, exist_ok=True)
    page = os.path.join(site, "detail.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(read("detail_gov.html"))

    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(_Q, directory=site))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        src = Source(key="t", name="本地静态页", type="html_list")
        src.detail.content_selectors = ["div.detail_content"]
        src.detail.tiers = ["html"]
        art = Article(url=f"{base}/detail.html", source="本地静态页", title="t")
        fetcher = Fetcher(NetConfig(timeout=10, retries=0, sleep_between=0, use_proxy=False))
        fetch_article(src, art, fetcher)
        fetcher.close()
        check("T27 状态为 full", art.fetch_status == FetchStatus.FULL.value,
              f"{art.fetch_status} / {art.notes[:120]}")
        check("T27 取到正文", "500亿元" in art.text, art.text[:80])
        check("T27 文号被抽取", art.docno == "沪府办发〔2026〕18号", art.docno)
        check("T27 生效日期被抽取", art.effective_date == "2026-10-01", str(art.effective_date))
    finally:
        httpd.shutdown()
        httpd.server_close()


@test("T28 回归：dry-run 不得依赖 httpx/lxml（缺依赖的用户最该先跑它）")
def t28() -> None:
    """这是实测发现的缺陷。

    原实现把 dry-run 计划放在 run.py 里，而 run.py 顶部 import 了 httpx/lxml，
    于是「缺依赖时最该先跑」的 `collect --dry-run` 自己就抛 ImportError。
    正确做法是让计划逻辑只依赖 pyyaml（读配置）。
    这里做静态检查：dryrun.py 源码不得出现 httpx / lxml / net / fetch / store 的导入。
    """
    src_path = os.path.join(TOOLS, "collector", "dryrun.py")
    check("T28 dryrun.py 存在", os.path.isfile(src_path), src_path)

    # 用 AST 解析而不是子串匹配：文档字符串里也会提到 "import httpx"，
    # 朴素匹配会误报（实测被自己的测试抓到）。
    import ast

    with open(src_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"httpx", "lxml", "net", "fetch", "store", "listparse"}
    check("T28 未导入 httpx/lxml/net/fetch/store",
          not (imported & forbidden),
          f"实际导入: {sorted(imported)}")
    check("T28 只依赖 registry 与标准库",
          imported <= {"registry", "typing", "__future__"},
          f"实际导入: {sorted(imported)}")

    # 并且它必须真的能产出计划
    from collector import dryrun

    plan = dryrun.build_dry_run_plan(SRC_YAML)
    check("T28 可生成计划", plan.get("sources_selected", 0) >= 1, str(plan)[:120])
    check("T28 计划含请求地址",
          all(p["listing_requests"] for p in plan["plan"]),
          str(plan["plan"])[:120])
    check("T28 schema 与正常路径一致",
          {"failures", "duplicates", "archive_path"} <= set(plan), str(sorted(plan)))


@test("T23 完整性四态与结论强度绑定")
def t23() -> None:
    check("T23 full 可支撑核心结论", FetchStatus.FULL.allows_core_conclusion)
    check("T23 partial 不可支撑核心结论", not FetchStatus.PARTIAL.allows_core_conclusion)
    check("T23 listing_only 不可支撑结论", not FetchStatus.LISTING_ONLY.allows_any_conclusion)
    check("T23 blocked 不可支撑结论", not FetchStatus.BLOCKED.allows_any_conclusion)
    check("T23 scanned_pdf 不可支撑结论", not FetchStatus.SCANNED_PDF.allows_any_conclusion)


# ------------------------------------------------------------------ 运行
def main() -> int:
    print(f"运行测试：{len(PASSED) + len(FAILED)} 项断言组")
    print("-" * 72)
    for name in PASSED:
        print(f"  PASS  {name}")
    for name, detail in FAILED:
        print(f"  FAIL  {name}")
        if detail:
            print("        " + detail.replace("\n", "\n        ")[:1500])
    print("-" * 72)
    print(f"通过 {len(PASSED)} 项 / 失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for name, detail in FAILED:
            print(f"  - {name}: {detail[:300]}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
