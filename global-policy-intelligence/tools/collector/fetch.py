# -*- coding: utf-8 -*-
"""collector · 正文抓取回落链（决策树状态机）

   ① api    ── 信源有官方 JSON 接口 → 精确取正文与文号（最稳）
   ② text   ── 官方纯文本/XML 正文地址（详情页 HTML 里没有正文的站点靠这一级）
   ③ html   ── 静态 HTML + 选择器 + 最长块启发式
   ④ pdf    ── URL 本身是 PDF，或 HTML 里指向 PDF 附件
   ⑤ render ── 静态为空但疑似 JS → 交给外部渲染器（POLICY_INTEL_RENDER_CMD）
   ⑥ 明确失败 ── 标 blocked + 原因 + 尝试记录，绝不产出结论

设计要点：
  · 每个级只负责「把候选正文交出来」，去噪/判级/抽元数据统一走 postprocess()。
    早期实现让 5 个级各自重复这段胶水代码（4 处几乎相同的 20 行），既臃肿又容易不一致。
  · 不设 mirror（存档镜像）级：它成功率低、在国内网络基本不可达，却带来 65 行代码
    与一堆"必须用 id_ 原始地址否则拿到工具栏页"的踩坑逻辑。抓不到就老实标 blocked——
    这更符合本工具"拿不到正文就不出结论"的立场。
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from . import extract as ex
from . import pdfx
from .models import Article, FetchStatus
from .net import Fetcher
from .registry import Source, _pick

# 详情页 URL 里常见的 ID 参数名，用于自动试探同域 JSON 接口
_ID_PARAM = re.compile(r"[?&](docId|docid|id|articleId|article_id|contentId|infoId|fileId)=([\w-]+)")
_BODY_KEYS = (
    "docClob|docContent|content|docHtml|body|text|articleContent|docText|"
    "contentHtml|mainContent|zwcontent"
)
_TITLE_KEYS = "docTitle|title|document_title_t"
_DATE_KEYS = "publishDate|pubDate|docEditdate|date|mas_date_tdt"
_DOCNO_KEYS = "documentNo|docNo|wenhao|file_code"
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


class _SafeFields(dict):
    """URL 模板缺字段时留空，而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return ""


# ------------------------------------------------------------------ 共用后处理
def postprocess(
    source: Source,
    article: Article,
    text: str,
    *,
    method: str,
    html_for_status: str = "",
    reasons: dict[str, str] | None = None,
    title: str = "",
    date: str = "",
    docno: str = "",
    extra: dict[str, str] | None = None,
) -> FetchOutcome:
    """把各级交出来的原始正文统一成 FetchOutcome。

    这一处取代了原先在 5 个 _tier_* 里各写一遍的「判级 → 抽标题/日期/文号/机关/生效日」。
    reasons 可为特定状态提供更具体的说明（如 pdf 的"无文字层，需 OCR"）。
    """
    status, default_reason = ex.classify_status(
        html_for_status, text, source.min_body_chars, source.max_body_chars
    )
    if status == FetchStatus.FULL.value and method in (
        "html:heuristic", "html:fallback", "render:heuristic", "render:fallback"
    ):
        status = FetchStatus.PARTIAL.value
        default_reason = "仅启发式抽取，正文边界与完整性需人工复核"
    reason = (reasons or {}).get(status, default_reason)
    outcome = FetchOutcome(
        text=text,
        status=status,
        method=method,
        reason=reason,
        title=title or article.title,
        date=ex.parse_any_date(date) or article.date or ex.parse_publication_date(text),
        docno=docno or ex.extract_docno(text) or article.docno,
        issuer=ex.extract_issuer(text),
        effective_date=ex.parse_effective_date(text),
    )
    if extra:
        for key, value in extra.items():
            setattr(outcome, key, value)
    return outcome


def _blocked(method: str, reason: str) -> FetchOutcome:
    return FetchOutcome(text="", status=FetchStatus.BLOCKED.value, method=method, reason=reason)


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


def _template_fields(article: Article) -> dict[str, str]:
    """构造模板渲染用的字段集合：列表原始字段 + URL 参数 + 已抽取的元数据。"""
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
    for key, values in parse_qs(urlsplit(article.url).query).items():
        if values:
            fields.setdefault(key, values[0])
            fields.setdefault(key.lower(), values[0])
    return fields


def _resolve_api_url(api_tpl: str, article: Article) -> str | None:
    """把正文接口模板渲染成真实 URL。

    策略（按可靠性排序）：
      ① 列表记录字段 / URL 查询参数填充模板（最可靠）
      ② 若仍有占位符没填上，再从 URL 里提取数字/ID 逐一尝试替换
    这样既不依赖 URL 一定含 `?docId=`，也能覆盖路径式详情页。
    """
    fields = _template_fields(article)
    try:
        return api_tpl.format_map(fields)
    except (KeyError, ValueError):
        return None


# ------------------------------------------------------------------ 各级实现
def _tier_api(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """官方 JSON 接口级：字段结构化，最稳。"""
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

    text = html_fragment_to_text(str(_pick(record, source.detail.body_field or _BODY_KEYS) or ""))
    title = str(_pick(record, _TITLE_KEYS) or "")
    date = str(_pick(record, _DATE_KEYS) or "")[:10]
    docno = str(_pick(record, _DOCNO_KEYS) or "")
    # 接口返回的文号/标题往往在正文块之外（正文 div 里没有文号）。必须并入快照正文——
    # 否则报告引用文号时校验器在证据里找不到出处，会把正确引用误判成编造（实测 C3 报到过）。
    header_bits = [b for b in (title, docno) if b and b not in text]
    if header_bits:
        text = "\n".join(header_bits) + "\n" + text

    outcome = postprocess(source, article, text, method="api",
                          title=title, date=date, docno=docno)
    if outcome.status == FetchStatus.LISTING_ONLY.value:
        return None      # 接口通了但没正文 → 继续往下走
    return outcome


def _tier_text(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """纯文本正文级。

    很多官方站点把正文以纯文本或 XML 单独提供（例如美国联邦公报的 raw_text_url）。
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
    params.update({"docno": docno, "document_number": docno, "date": date,
                   "year": date[:4], "month": date[5:7], "day": date[8:10]})

    # 先问接口要确切地址（比拼接可靠：能规避非工作日发布、编号不规则等情况）
    candidates: list[str] = []
    if api_tpl:
        meta = fetcher.get_json(api_tpl.format_map(_SafeFields(params)), tier="api")
        if isinstance(meta, dict):
            for key in ("raw_text_url", "text_url", "full_text_url"):
                value = meta.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    candidates.append(value)
                    break
    if tpl:
        built = tpl.format_map(_SafeFields(params))
        if built.startswith("http"):
            candidates.append(built)

    for text_url in candidates:
        raw = fetcher.get_text(text_url, tier="text")
        if not raw or not raw.strip():
            continue
        text = _plain_text_body(raw)
        if len(text) < source.min_body_chars:
            continue
        outcome = postprocess(source, article, text, method="text")
        if outcome.status in (FetchStatus.LISTING_ONLY.value, FetchStatus.BLOCKED.value):
            continue
        return outcome
    return None


def _plain_text_body(raw: str) -> str:
    """把"纯文本"接口的返回转成正文。

    实测 Federal Register 的 .txt 其实是 <html><body><pre>…</pre></body></html>，
    所以先尝试解析、再兜底当纯文本；不能因为出现 <html 就否掉一个真实语料。
    """
    head = raw[:2000].lower()
    if "<pre" in head or "<html" in head:
        tree = ex.parse_html(raw)
        if tree is not None:
            ex.strip_noise(tree)
            pres = tree.xpath("//pre")
            parsed = ex.normalize_text(
                "\n".join(p.text_content() for p in pres) if pres else tree.text_content()
            )
            if parsed:
                return parsed
    return ex.normalize_text(re.sub(r"<[^>]+>", " ", raw))


def _tier_html(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """静态 HTML 级：选择器 + 最长文本块启发式。"""
    got = fetcher.get_document(article.url, tier="html", encoding=source.listing.encoding)
    if got is None:
        return None
    kind, payload = got
    if kind == "pdf":
        # 详情页其实返回 PDF：绝不当正文用，交给 pdf 级处理二进制。
        return FetchOutcome(
            text="", status=FetchStatus.LISTING_ONLY.value,
            method="html:skipped(pdf)", reason="详情页为 PDF，转由 pdf 级解析",
        )
    html = payload
    tree = ex.parse_html(html)
    if tree is None:
        return None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, source.detail.content_selectors, source.min_body_chars)
    outcome = postprocess(
        source, article, text,
        method=f"html:{how}",
        html_for_status=html,
        title=ex.extract_title(tree),
    )
    return outcome


def _tier_pdf(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """PDF 级：URL 本身是 PDF，或 HTML 里指向 PDF 附件。"""
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
                for sel in source.detail.pdf_selectors or ["//a[@href]"]:
                    nodes = (tree.xpath(sel) if sel.startswith(("/", "a[@"))
                             else ex.select_nodes(tree, sel))
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
    raw_text, method = pdfx.extract_pdf_text(data)
    if method == "scanned":
        return FetchOutcome(
            text=pdfx.normalize_pdf_text(raw_text),
            status=FetchStatus.SCANNED_PDF.value, method="pdf:scanned",
            reason="PDF 无文字层（扫描件），需 OCR/VLM",
        )
    if method.startswith("failed") or not raw_text.strip():
        return _blocked("pdf:failed", f"PDF 解析失败（{method}）")
    text = ex.normalize_text(pdfx.normalize_pdf_text(raw_text))
    outcome = postprocess(source, article, text, method=f"pdf:{method}")
    if method == "pypdf:partial" and outcome.status == FetchStatus.FULL.value:
        outcome.status = FetchStatus.PARTIAL.value
        outcome.reason = "PDF 含未识别扫描页或文字层不完整，须补充 OCR 并复核"
    return outcome


def _tier_render(source: Source, article: Article, fetcher: Fetcher) -> FetchOutcome | None:
    """渲染级：调用外部渲染器（若已配置）。

    渲染器约定：接受一个 URL 参数，把渲染后的 HTML 打到 stdout。
    """
    cmd = os.environ.get("POLICY_INTEL_RENDER_CMD", "")
    if not cmd:
        return None
    try:
        proc = subprocess.run([cmd, article.url], capture_output=True, timeout=90, check=False)
        if proc.returncode != 0:
            return _blocked("render", f"渲染器退出码 {proc.returncode}")
        html = (proc.stdout or b"").decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return _blocked("render", f"渲染器执行失败: {exc}")
    if not html.strip():
        return None
    tree = ex.parse_html(html)
    if tree is None:
        return None
    ex.strip_noise(tree)
    text, how = ex.extract_body(tree, source.detail.content_selectors, source.min_body_chars)
    outcome = postprocess(source, article, text, method=f"render:{how}",
                          html_for_status=html, title=ex.extract_title(tree))
    # 渲染后仍拿不到正文：不当作"成功但没内容"，交给更靠后的级
    return None if outcome.status == FetchStatus.LISTING_ONLY.value else outcome


_TIERS = {
    "api": _tier_api,
    "text": _tier_text,
    "html": _tier_html,
    "pdf": _tier_pdf,
    "render": _tier_render,
}


# ------------------------------------------------------------------ 对外接口
def fetch_article(source: Source, article: Article, fetcher: Fetcher) -> Article:
    """按回落链抓一条正文，就地升级 article 的字段与状态。"""
    attempt_start = len(fetcher.attempts)
    if source.detail.mode == "blocked":
        article.fetch_status = FetchStatus.BLOCKED.value
        article.fetch_method = "blocked"
        article.notes = source.detail.blocked_reason or "信源声明为不可自动获取"
        article.attempts = []
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
        if outcome is None:
            continue
        # blocked 只作为兜底信息，不阻断后续可能更有效的级
        if outcome.status == FetchStatus.BLOCKED.value:
            best = best or outcome
            continue
        if outcome.status == FetchStatus.FULL.value:
            best = outcome
            break
        rank = {"partial": 3, "scanned_pdf": 2, "listing_only": 1, "blocked": 0}
        if best is None or rank.get(outcome.status, 0) > rank.get(best.status, 0):
            best = outcome

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
    article.attempts = [a.to_dict() for a in fetcher.attempts[attempt_start:]]
    return article
