# -*- coding: utf-8 -*-
"""collector · 正文抓取回落链（决策树状态机）

   ① api    ── 信源有官方 JSON 接口 → 精确取正文与文号（最稳）
   ② html   ── 静态 HTML + 选择器 + 最长块启发式
   ③ pdf    ── URL 本身是 PDF，或 HTML 里指向 PDF 附件
   ④ render ── 静态为空但疑似 JS → 交给外部渲染器（opencli render）
   ⑤ mirror ── 存档镜像兜底（Internet Archive）
   ⑥ 明确失败 ── 标 blocked + 原因 + 尝试记录，绝不产出结论

每一级都写入 attempts，超预算即降级。这是「证明真的努力过，再判无动态」的实现。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import lxml.html as LH

from . import extract as ex
from . import pdfx
from .models import Article, FetchStatus
from .net import Fetcher
from .registry import Source

# 详情页 URL 里常见的 ID 参数名，用于自动试探同域 JSON 接口
_ID_PARAM = re.compile(r"[?&](docId|docid|id|articleId|article_id|contentId|infoId|fileId)=([\w-]+)")
_BODY_KEYS = (
    "docClob|docContent|content|docHtml|body|text|articleContent|docText|"
    "contentHtml|mainContent|zwcontent"
)
_HTML_TAG = re.compile(r"<(p|div|br|span|td|table|h[1-6]|img)\b", re.I)


@dataclass
class FetchOutcome:
    text: str = ""
    status: str = FetchStatus.LISTING_ONLY.value
    method: str = ""
    reason: str = ""
    title: str = ""
    date: str | None = None
    docno: str = ""
    issuer: str = ""
    effective_date: str | None = None
    pdf_method: str = ""


class _SafeFields(dict):
    """URL 模板缺字段时留空，而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return ""


# ------------------------------------------------------------------ 小工具
def html_fragment_to_text(fragment: str) -> str:
    """JSON 接口返回的正文常常是一段 HTML，需要转纯文本。"""
    if not fragment:
        return ""
    if not _HTML_TAG.search(fragment):
        return ex.normalize_text(fragment)
    tree = ex.parse_html(f"<div id='__frag__'>{fragment}</div>")
    if tree is None:
        return ex.normalize_text(re.sub(r"<[^>]+>", " ", fragment))
    nodes = tree.xpath("//div[@id='__frag__']")
    target = nodes[0] if nodes else tree
    ex.strip_noise(target)
    return ex.normalize_text(target.text_content())


def _extract_id(url: str) -> tuple[str, str] | None:
    m = _ID_PARAM.search(url)
    return (m.group(1), m.group(2)) if m else None


def _looks_like_pdf_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.endswith(".pdf") or "/resource/file?" in lowered or ".pdf?" in lowered


# ------------------------------------------------------------------ 各级实现
def _tier_text(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """纯文本正文级。

    很多政府/官方站点把正文以纯文本或 XML 形式单独提供（例如美国联邦公报的
    raw_text_url：https://www.federalregister.gov/documents/full_text/text/...txt）。
    这类地址可靠、无版式噪声，优先级应高于 HTML 解析——实测 Federal Register
    的详情页 HTML 里**根本没有正文**，只有 raw_text_url 有。
    """
    tpl = source.detail.text_url_template
    api_tpl = source.detail.text_api_template
    if not tpl and not api_tpl:
        return None
    params = _template_fields(article)
    params.setdefault("url", article.url)
    docno = article.docno or ""
    date = article.date or ""
    params.update(
        {
            "docno": docno,
            "document_number": docno,
            "date": date,
            "year": date[:4],
            "month": date[5:7],
            "day": date[8:10],
        }
    )

    # 先问接口要确切地址（比拼接可靠：能规避非工作日发布、编号不规则等情况）
    candidates: list[str] = []
    if api_tpl:
        try:
            meta = fetcher.get_json(api_tpl.format_map(_SafeFields(params)), tier="api")
        except Exception:  # noqa: BLE001
            meta = None
        if isinstance(meta, dict):
            for key in ("raw_text_url", "text_url", "full_text_url"):
                value = meta.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    candidates.append(value)
                    break
    if tpl:
        try:
            built = tpl.format_map(_SafeFields(params))
            if built.startswith("http"):
                candidates.append(built)
        except Exception:  # noqa: BLE001
            pass

    for text_url in candidates:
        raw = fetcher.get_text(text_url, tier="text")
        if not raw or not raw.strip():
            continue
        # 有的"纯文本"接口其实是包在 HTML 里的（实测 Federal Register 的 .txt 就是
        # <html><body><pre>…</pre></body></html>）。所以先尝试解析，再兜底当纯文本用；
        # 只有真的解析不出内容才放弃，不能因为出现 <html 就否掉一个真实语料。
        text = ""
        if "<pre" in raw[:2000].lower() or "<html" in raw[:2000].lower():
            tree = ex.parse_html(raw)
            if tree is not None:
                ex.strip_noise(tree)
                pres = tree.xpath("//pre")
                text = ex.normalize_text(
                    "\n".join(p.text_content() for p in pres) if pres else tree.text_content()
                )
        if not text:
            text = ex.normalize_text(re.sub(r"<[^>]+>", " ", raw))
        if len(text) < source.min_body_chars:
            continue
        status, reason = ex.classify_status("", text, source.min_body_chars, source.max_body_chars)
        if status in (FetchStatus.LISTING_ONLY.value, FetchStatus.BLOCKED.value):
            continue
        return FetchOutcome(
            text=text,
            status=status,
            method="text",
            reason=reason,
            title=article.title,
            date=ex.parse_any_date(text) or article.date,
            docno=ex.extract_docno(text) or article.docno,
            issuer=ex.extract_issuer(text),
            effective_date=ex.parse_effective_date(text),
        )
    return None


def _template_fields(article: Article) -> dict[str, str]:
    """构造模板渲染用的字段集合：列表原始字段 + URL 中的参数 + 已抽取的元数据。"""
    fields: dict[str, str] = {}
    for key, value in (article.params or {}).items():
        if isinstance(value, (str, int, float)):
            fields[key] = str(value)
    # 大小写不敏感别名（docId / docid / DOCID 都能用）
    for key, value in list(fields.items()):
        fields.setdefault(key.lower(), value)
    if article.docno:
        fields.setdefault("docno", article.docno)
        fields.setdefault("documentNo", article.docno)
    if article.date:
        fields.setdefault("date", article.date)
        fields.setdefault("year", article.date[:4])
        fields.setdefault("month", article.date[5:7])
        fields.setdefault("day", article.date[8:10])
    # URL 里的查询参数也并进来
    from urllib.parse import parse_qs, urlsplit

    for key, values in parse_qs(urlsplit(article.url).query).items():
        if values:
            fields.setdefault(key, values[0])
            fields.setdefault(key.lower(), values[0])
    return fields


def _resolve_api_url(api_tpl: str, article: Article) -> str | None:
    """把正文接口模板渲染成真实 URL。

    策略（按可靠性排序）：
      ① 列表记录字段 / URL 查询参数填充模板（最可靠）
      ② 若模板里仍有占位符没填上，再从 URL 里提取数字/ID 逐一尝试替换
    这样既不依赖 URL 一定含 `?docId=`，也能覆盖路径式详情页。
    """
    fields = _template_fields(article)
    try:
        filled = api_tpl.format_map(_SafeFields(fields))
    except Exception:  # noqa: BLE001
        return None
    if "{" not in filled:
        return filled

    # ② 回退：把未填的占位符用 URL 中出现的候选 ID 替换
    ids: list[str] = []
    got = _extract_id(article.url)
    if got:
        ids.append(got[1])
    ids += re.findall(r"(\d{3,})", article.url)
    ids += re.findall(r"([0-9a-f]{8,})", article.url.lower())
    for candidate in ids:
        probe_fields = dict(fields)
        for name in re.findall(r"\{(\w+)\}", api_tpl):
            probe_fields.setdefault(name, candidate)
        try:
            resolved = api_tpl.format_map(_SafeFields(probe_fields))
        except Exception:  # noqa: BLE001
            continue
        if "{" not in resolved:
            return resolved
    return None


def _tier_api(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    api_tpl = source.detail.api_url
    if not api_tpl:
        return None
    api_url = _resolve_api_url(api_tpl, article)
    if not api_url:
        return None
    payload = fetcher.get_json(api_url, tier="api")
    if payload is None:
        return None
    record = payload
    if isinstance(record, dict) and isinstance(record.get("data"), (dict, list)):
        record = record["data"]
    if isinstance(record, list):
        record = record[0] if record else None
    if not isinstance(record, dict):
        return None

    from .registry import _pick

    body_raw = _pick(record, source.detail.body_field or _BODY_KEYS)
    text = html_fragment_to_text(str(body_raw or ""))
    title = str(_pick(record, "docTitle|title|document_title_t") or "") or article.title
    date = str(_pick(record, "publishDate|pubDate|docEditdate|date|mas_date_tdt") or "")[:10]
    docno = str(_pick(record, "documentNo|docNo|wenhao|file_code") or "")
    # 接口返回的文号/标题往往在正文块之外（正文 div 里没有文号）。
    # 必须把它们并入快照正文——否则报告引用文号时，校验器在证据里找不到出处，
    # 会把正确引用误判成编造（实测 C3 就是这样报出来的）。
    header_bits = [b for b in (title, docno) if b and b not in text]
    if header_bits:
        text = "\n".join(header_bits) + "\n" + text
    status, reason = ex.classify_status("", text, source.min_body_chars, source.max_body_chars)
    if status == FetchStatus.LISTING_ONLY.value:
        return None      # 接口通了但没正文 → 继续往下走
    return FetchOutcome(
        text=text,
        status=status,
        method="api",
        reason=reason,
        title=title or article.title,
        date=ex.parse_any_date(date) or article.date,
        docno=docno or article.docno,
        issuer=ex.extract_issuer(text),
        effective_date=ex.parse_effective_date(text),
    )


def _tier_html(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    got = fetcher.get_document(article.url, tier="html", encoding=source.listing.encoding)
    if got is None:
        return None
    kind, payload = got
    if kind == "pdf":
        # 详情页其实返回 PDF：绝不当正文用，转交 pdf 级处理二进制。
        return FetchOutcome(
            text="",
            status=FetchStatus.LISTING_ONLY.value,
            method="html:skipped(pdf)",
            reason="详情页为 PDF，转由 pdf 级解析",
        )
    html = payload
    tree = ex.parse_html(html)
    if tree is None:
        return None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, source.detail.content_selectors, source.min_body_chars)
    title = ex.extract_title(tree) or article.title
    status, reason = ex.classify_status(html, text, source.min_body_chars, source.max_body_chars)
    if status == FetchStatus.BLOCKED.value:
        return FetchOutcome(
            text="", status=status, method="html", reason=reason, title=title
        )
    if status == FetchStatus.LISTING_ONLY.value:
        # 静态没拿到，但先看看页面上有没有 PDF 附件（由 pdf 级处理）
        return FetchOutcome(
            text=text, status=status, method=f"html:{how}", reason=reason, title=title
        )
    return FetchOutcome(
        text=text,
        status=status,
        method=f"html:{how}",
        reason=reason,
        title=title,
        date=ex.parse_any_date(text) or article.date,
        docno=ex.extract_docno(text),
        issuer=ex.extract_issuer(text),
        effective_date=ex.parse_effective_date(text),
    )


def _tier_pdf(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    pdf_url = ""
    if _looks_like_pdf_url(article.url):
        pdf_url = article.url
    else:
        got = fetcher.get_document(article.url, tier="html")
        if got is None:
            return None
        kind, payload = got
        if kind == "pdf":
            pdf_url = article.url          # 详情页本身返回 PDF（实测 UK legislation 如此）
        else:
            tree = ex.parse_html(payload)
            if tree is not None:
                selectors = source.detail.pdf_selectors or ["//a[@href]"]
                for sel in selectors:
                    nodes = (
                        tree.xpath(sel) if sel.startswith(("/", "a[@"))
                        else ex.select_nodes(tree, sel)
                    )
                    for node in nodes:
                        href = node.get("href") or ""
                        if _looks_like_pdf_url(href):
                            from .listparse import absolutize

                            pdf_url = absolutize(article.url, href)
                            break
                    if pdf_url:
                        break
    if not pdf_url:
        return None
    data = fetcher.get_bytes(pdf_url, tier="pdf")
    if not data:
        return None
    text, method = pdfx.extract_pdf_text(data)
    if method == "scanned":
        return FetchOutcome(
            text=pdfx.normalize_pdf_text(text),
            status=FetchStatus.SCANNED_PDF.value,
            method="pdf:scanned",
            reason="PDF 无文字层（扫描件），需 OCR/VLM",
        )
    if method.startswith("failed") or not text.strip():
        return FetchOutcome(
            text="", status=FetchStatus.BLOCKED.value, method="pdf:failed",
            reason=f"PDF 解析失败（{method}）",
        )
    text = ex.normalize_text(pdfx.normalize_pdf_text(text))
    status, reason = ex.classify_status("", text, source.min_body_chars, source.max_body_chars)
    return FetchOutcome(
        text=text,
        status=status,
        method=f"pdf:{method}",
        reason=reason,
        title=article.title,
        date=ex.parse_any_date(text) or article.date,
        docno=ex.extract_docno(text),
        issuer=ex.extract_issuer(text),
        effective_date=ex.parse_effective_date(text),
        pdf_method=method,
    )


def _tier_render(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """调用外部渲染器（若已配置）。渲染器约定：接受 URL，把渲染后的 HTML 打到 stdout。"""
    cmd = os.environ.get("POLICY_INTEL_RENDER_CMD", "")
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            [cmd, article.url],
            capture_output=True,
            timeout=90,
            check=False,
        )
        html = (proc.stdout or b"").decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(text="", status=FetchStatus.BLOCKED.value, method="render",
                            reason=f"渲染器执行失败: {exc}")
    if not html.strip():
        return None
    tree = ex.parse_html(html)
    if tree is None:
        return None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, source.detail.content_selectors, source.min_body_chars)
    status, reason = ex.classify_status(html, text, source.min_body_chars, source.max_body_chars)
    if status == FetchStatus.LISTING_ONLY.value:
        return None
    return FetchOutcome(
        text=text, status=status, method=f"render:{how}", reason=reason,
        title=ex.extract_title(tree) or article.title,
        date=ex.parse_any_date(text) or article.date,
        docno=ex.extract_docno(text),
        issuer=ex.extract_issuer(text),
        effective_date=ex.parse_effective_date(text),
    )


def _tier_mirror(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """存档镜像兜底：使用 Internet Archive 快照。

    两个必须遵守的纪律（都是实测踩出来的）：
      ① 必须用 `id_` 原始内容地址。默认快照地址返回的是 Wayback 工具栏页面，
         实测只有 207 字且全是短行链接，却越过了 200 字阈值被标成 full——
         等于拿"检索工具自己的界面文字"当政策正文。
      ② 镜像只作最后兜底，且结果里必须标明"经存档获取、原文站本次不可达"。
    """
    if os.environ.get("POLICY_INTEL_NO_MIRROR"):
        return None
    if "rate_limited" in _MIRROR_DISABLED:
        return None
    avail = fetcher.get_json(
        f"https://archive.org/wayback/available?url={article.url}", tier="mirror"
    )
    snap = ((avail or {}).get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available") or not snap.get("url"):
        return None

    # 优先原始内容地址（id_），其次原始 HTML 形参（if_）；最后才退回默认快照地址
    base = str(snap["url"])
    raw_url = base.replace("/http", "id_/http", 1) if "/http" in base else ""
    if_url = base + ("&if_" if "?" in base else "?if_")
    candidates = [u for u in (raw_url, if_url, base) if u]
    html = ""
    used = ""
    for candidate in candidates:
        if not candidate:
            continue
        got = fetcher.get_text(candidate, tier="mirror")
        if not got:
            continue
        # 命中镜像工具栏特征的一律丢弃，换下一个地址形态
        if ex.looks_like_nav_page(got, 0) or re.search(r"Wayback Machine", got, re.I):
            continue
        html, used = got, candidate
        break
    if not html:
        return None

    tree = ex.parse_html(html)
    if tree is None:
        return None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, source.detail.content_selectors, source.min_body_chars)
    status, reason = ex.classify_status(html, text, source.min_body_chars, source.max_body_chars)
    if status in (FetchStatus.LISTING_ONLY.value, FetchStatus.BLOCKED.value):
        return None
    return FetchOutcome(
        text=text, status=status, method=f"mirror:{how}",
        reason=f"经存档快照 {snap.get('timestamp', '')} 获取；原文站本次不可达（{used[:80]}）",
        title=ex.extract_title(tree) or article.title,
        date=ex.parse_any_date(text) or article.date,
        docno=ex.extract_docno(text),
        issuer=ex.extract_issuer(text),
        effective_date=ex.parse_effective_date(text),
    )


_TIERS = {
    "api": _tier_api,
    "text": _tier_text,
    "html": _tier_html,
    "pdf": _tier_pdf,
    "render": _tier_render,
    "mirror": _tier_mirror,
}

# 镜像源被限流时（429），在本次运行内直接停用镜像级，避免逐条浪费预算
_MIRROR_DISABLED: set[str] = set()


def disable_mirror(reason: str) -> None:
    _MIRROR_DISABLED.add(reason)


# ------------------------------------------------------------------ 对外接口
def fetch_article(source: Source, article: Article, fetcher: Fetcher) -> Article:
    """按回落链抓一条正文，就地升级 article 的字段与状态。"""
    if source.detail.mode == "blocked":
        article.fetch_status = FetchStatus.BLOCKED.value
        article.fetch_method = "blocked"
        article.notes = source.detail.blocked_reason or "信源声明为不可自动获取"
        article.attempts = [a.to_dict() for a in fetcher.attempts]
        return article

    best: FetchOutcome | None = None
    for tier_name in source.detail.resolved_tiers():
        fn = _TIERS.get(tier_name)
        if fn is None:
            continue
        try:
            outcome = fn(source, article, fetcher)
        except Exception as exc:  # noqa: BLE001
            import traceback

            article.notes = f"{tier_name} 级异常: {type(exc).__name__}: {exc}"[:200]
            if os.environ.get("POLICY_INTEL_DEBUG_TIERS"):
                traceback.print_exc()
            continue
        # 镜像被限流（429）→ 本次运行内停用镜像级，避免逐条白耗预算
        if tier_name == "mirror" and any(
            a.status == 429 and a.tier.startswith("mirror") for a in fetcher.attempts
        ):
            _MIRROR_DISABLED.add("rate_limited")
        if outcome is None:
            continue
        # blocked 只作为最后的信息，不阻断后续更有效的级
        if outcome.status == FetchStatus.BLOCKED.value:
            best = best or outcome
            continue
        if outcome.status in (FetchStatus.FULL.value, FetchStatus.PARTIAL.value):
            best = outcome
            break
        if outcome.status == FetchStatus.SCANNED_PDF.value:
            best = outcome
            break
        best = best or outcome

    if best is None:
        article.fetch_status = FetchStatus.BLOCKED.value
        article.fetch_method = "none"
        article.notes = "所有回落级均未取得正文"
    else:
        article.text = best.text
        article.fetch_status = best.status
        article.fetch_method = best.method
        article.title = best.title or article.title
        article.date = best.date or article.date
        article.docno = best.docno or article.docno
        article.issuer = best.issuer or article.issuer
        article.effective_date = best.effective_date or article.effective_date
        if best.reason and article.fetch_status != FetchStatus.FULL.value:
            article.notes = best.reason
    article.attempts = [a.to_dict() for a in fetcher.attempts]
    return article


def summarize_attempts(fetcher: Fetcher) -> list[dict[str, Any]]:
    return [a.to_dict() for a in fetcher.attempts]
