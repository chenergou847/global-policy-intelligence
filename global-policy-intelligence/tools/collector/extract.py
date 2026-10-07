# -*- coding: utf-8 -*-
"""collector · 正文与元数据抽取

这里是「只能看见题目」这个问题的技术核心。三级策略：

  ① 源声明的 content_selectors（政府站惯用 div.detail_content / TRS_Editor / #zoom / article）
  ② 最长文本块启发式（对未知版式兜底）
  ③ 全文兜底 + 噪声清理

并且**必须**对结果做完整性判定：JS 骨架页、验证页、导航页都会被识别为
非正文，而不是被当成正文送进分析。这一步是防幻觉的第一道闸门。
"""
from __future__ import annotations

import re
from typing import Iterable

import lxml.html as LH

from .models import FetchStatus

# ------------------------------------------------------------------ 噪声清理
_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "form", "iframe", "svg")

# 政府站常见的正文容器，按优先级排列
DEFAULT_CONTENT_SELECTORS: tuple[str, ...] = (
    "div.detail_content",
    "div.TRS_Editor",
    "div#zoom",
    "div#UCAP-CONTENT",
    "div.article-content",
    "div.article_content",
    "div.artical-content",
    "div.content_box",
    "div.pages_content",
    "div.xxgk_content",
    "div.zwxl-article",
    "div.view TRS_Editor",
    "article",
    "div.article",
    "div.content",
    "div.detail",
    "div.main-content",
    "main",
)

# 明显的非正文容器
_JUNK_SELECTOR_HINT = re.compile(
    r"(nav|menu|crumb|breadcrumb|share|comment|related|recommend|search|footer|header|"
    r"sidebar|banner|advert|广告|相关|推荐|分享|评论|导航|友情链接)",
    re.I,
)

# 元数据长串（base64 / 哈希 / 会话 ID），会污染正文与引文
_METADATA_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_WHITESPACE_RUN = re.compile(r"[ \t\u3000]+")

# 阻塞/验证页特征——命中即判 blocked，绝不当作正文
_BLOCK_PATTERNS = (
    re.compile(r"just a moment", re.I),
    re.compile(r"cf-browser-verification|cf_chl_|challenge-platform", re.I),
    re.compile(r"enable javascript and cookies to continue", re.I),
    re.compile(r"请开启\s*JavaScript", re.I),
    re.compile(r"访问验证|安全验证|人机验证|滑动验证|请输入验证码", re.I),
    re.compile(r"您的访问过于频繁|请求过于频繁|访问受限", re.I),
    re.compile(r"403\s*Forbidden|Access Denied|拒绝访问", re.I),
    re.compile(r"请先登录|登录后查看|扫码登录", re.I),
    re.compile(r"页面不存在|404\s*Not Found", re.I),
)

# JS 渲染骨架特征
_JS_ONLY_PATTERNS = (
    re.compile(r"<div[^>]+id=[\"'](app|root|__next|__nuxt)[\"'][^>]*>\s*</div>", re.I),
    re.compile(r"window\.__(NUXT|NEXT|INITIAL_STATE)__", re.I),
    re.compile(r"data-reactroot|ng-version=|v-cloak", re.I),
    re.compile(r"<script[^>]+src=[^>]*(main|chunk|app)\.[0-9a-f]{6,}\.js", re.I),
)

# 存档镜像/工具页的界面特征——命中的是"镜像工具本身"，不是被存档的原文
_MIRROR_CHROME = (
    re.compile(r"Wayback Machine", re.I),
    re.compile(r"\d+\s+captures?\b", re.I),
    re.compile(r"About this capture|TIMESTAMPS", re.I),
    re.compile(r"Save Page Now|web\.archive\.org/web/\d+/", re.I),
)

# 导航/索引页特征：短行占比极高，说明拿到的是链接清单而不是正文
_NAV_HINT_LINES = 12
_NAV_SHORT_LINE = 12


def looks_like_nav_page(text: str, min_chars: int = 200) -> bool:
    """判断是否拿到的是导航/索引页而非正文。

    实测踩到：Wayback 工具栏页只有 207 字，全是短行链接，却越过了 200 字阈值
    被标成 full。这类"假正文"必须挡掉，否则报告会引用检索工具自己的界面文字。
    """
    body = (text or "").strip()
    if not body:
        return False

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines:
        short = sum(1 for ln in lines if len(ln) <= _NAV_SHORT_LINE)
        if len(lines) >= _NAV_HINT_LINES and short / len(lines) >= 0.8:
            return True

    # 整页都是镜像工具界面
    hits = sum(1 for rx in _MIRROR_CHROME if rx.search(body[:4000]))
    if hits >= 2 and len(body) < max(min_chars * 2, 500):
        return True
    return False


# ------------------------------------------------------------------ 基础工具
# ------------------------------------------------------------------ 选择器
# 本环境没有 cssselect（lxml 的 cssselect 依赖它），因此实现一个最小 CSS→XPath
# 转换器，只覆盖信源配置里真实会用到的选择器形态。完全外部依赖为零。
_CSS_TOKEN = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*)?"
    r"(?P<id>#[\w-]+)?"
    r"(?P<cls>(?:\.[\w-]+)*)$"
)


def css_to_xpath(selector: str) -> str | None:
    """把 `div.view TRS_Editor` / `div#zoom` / `article` 这类选择器转成 XPath。"""
    selector = (selector or "").strip()
    if not selector:
        return None
    parts: list[str] = []
    for chunk in selector.split():
        if chunk in (">", "+", "~") or "[" in chunk or ":" in chunk:
            return None  # 形态过于复杂，交给启发式兜底
        m = _CSS_TOKEN.match(chunk)
        if not m:
            return None
        preds: list[str] = []
        tag = m.group("tag") or "*"
        if m.group("id"):
            preds.append(f"@id='{m.group('id')[1:]}'")
        for cls in [c for c in (m.group("cls") or "").split(".") if c]:
            preds.append(
                f"contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')"
            )
        parts.append(tag + ("[" + " and ".join(preds) + "]" if preds else ""))
    return "//" + "/".join(parts)


def select_nodes(tree: LH.HtmlElement, selector: str) -> list[LH.HtmlElement]:
    """选择节点：优先 cssselect（若装了 cssselect），否则走内置转换。"""
    if not selector or not selector.strip():
        return []
    try:
        return list(tree.cssselect(selector))
    except Exception:  # noqa: BLE001  ImportError / SelectorSyntaxError 都走兜底
        pass
    xp = css_to_xpath(selector)
    if not xp:
        return []
    try:
        return list(tree.xpath(xp))
    except Exception:  # noqa: BLE001
        return []


# lxml 的硬坑：若传入**含编码声明**的 unicode 字符串，fromstring 会抛
#   TypeError: a bytes-like object is required, not 'str'
# 政府/新闻页面几乎都有 <meta charset=...>，所以这一条不处理会导致
# **所有静态 HTML 源静默降级为 blocked**（实测踩到，且被上层 try 吞掉）。
_META_CHARSET = re.compile(r"<meta[^>]*charset[^>]*>", re.I)


def parse_html(html: str) -> LH.HtmlElement | None:
    if not html or not html.strip():
        return None
    # 先直接解析（大多数情况可用）
    try:
        return LH.fromstring(html)
    except Exception:  # noqa: BLE001
        pass
    # 去掉编码声明后再解析
    try:
        return LH.fromstring(_META_CHARSET.sub("", html, count=3))
    except Exception:  # noqa: BLE001
        pass
    # 最后退回字节解析
    for enc in ("utf-8", "gb18030"):
        try:
            parser = LH.HTMLParser(recover=True, encoding=enc)
            return LH.fromstring(html.encode(enc, errors="replace"), parser=parser)
        except Exception:  # noqa: BLE001
            continue
    return None


def strip_noise(tree: LH.HtmlElement) -> None:
    for tag in _NOISE_TAGS:
        for node in tree.xpath(f"//{tag}"):
            node.drop_tree()


def normalize_text(text: str) -> str:
    """清理正文：去元数据长串、压缩空白、去零宽字符。"""
    if not text:
        return ""
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = _METADATA_BLOB.sub(" ", text)
    lines = [_WHITESPACE_RUN.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _node_text(node: LH.HtmlElement) -> str:
    try:
        raw = node.text_content()
    except Exception:  # noqa: BLE001
        raw = "".join(node.itertext())
    return normalize_text(raw)


# ------------------------------------------------------------------ 标题
def extract_title(tree: LH.HtmlElement, declared: str | None = None) -> str:
    """标题三级回落：声明的选择器 → h1 → og:title → <title>（去站点后缀）。"""
    if declared:
        node = select_nodes(tree, declared)
        if node:
            t = _node_text(node[0])
            if t:
                return t[:200]
    for xp in ("//h1", "//*[@class='article-title']", "//*[@class='title']"):
        found = tree.xpath(xp)
        if found:
            t = _node_text(found[0])
            if t:
                return t[:200]
    og = tree.xpath("//meta[@property='og:title']/@content")
    if og and og[0].strip():
        return og[0].strip()[:200]
    if tree.find(".//title") is not None:
        t = normalize_text(tree.find(".//title").text_content())
        # 去掉 "标题 - 站点名" / "标题_站点名" 这类后缀
        t = re.split(r"\s*[|\-–—]\s*", t)[0] if re.search(r"[|\-–—]", t) else t
        return t.strip()[:200]
    return ""


# ------------------------------------------------------------------ 正文
def extract_body(
    tree: LH.HtmlElement,
    selectors: Iterable[str] | None = None,
    min_chars: int = 200,
) -> tuple[str, str]:
    """返回 (正文, 命中的选择器或 'heuristic'/'fallback')。"""
    candidates = list(selectors or ()) + [
        s for s in DEFAULT_CONTENT_SELECTORS if s not in (selectors or ())
    ]
    for sel in candidates:
        for node in select_nodes(tree, sel):
            if _JUNK_SELECTOR_HINT.search(sel) or _JUNK_SELECTOR_HINT.search(
                node.get("class", "") + " " + node.get("id", "")
            ):
                continue
            text = _node_text(node)
            if len(text) >= min_chars:
                return text, sel

    # 启发式：找「长度合理且最长」的块，排除导航/侧栏
    best, best_len = "", 0
    for node in tree.xpath("//div|//article|//section|//td"):
        cls_id = f"{node.get('class', '')} {node.get('id', '')}"
        if _JUNK_SELECTOR_HINT.search(cls_id):
            continue
        text = _node_text(node)
        n = len(text)
        if min_chars <= n <= 60000 and n > best_len:
            best, best_len = text, n
    if best:
        return best, "heuristic"

    # 兜底：整页文本
    return _node_text(tree), "fallback"


# ------------------------------------------------------------------ 日期
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_LABELED = re.compile(
    r"(?:发布时间|发布日期|成文日期|印发日期|发文日期|时间|日期|发布|更新日期|"
    r"published|posted|date)[^\d]{0,12}"
    r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})",
    re.I,
)
_DATE_CN = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_DASH = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_DATE_SLASH = re.compile(r"(20\d{2})/(\d{1,2})/(\d{1,2})")
_DATE_EN = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\.?\s*,?\s+(20\d{2})\b", re.I
)
_DATE_EN2 = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})\s*,?\s+(20\d{2})\b", re.I
)


def _mk(y: str | int, m: str | int, d: str | int) -> str | None:
    try:
        yi, mi, di = int(y), int(m), int(d)
    except (TypeError, ValueError):
        return None
    if not (2000 <= yi <= 2100 and 1 <= mi <= 12 and 1 <= di <= 31):
        return None
    return f"{yi:04d}-{mi:02d}-{di:02d}"


def parse_any_date(text: str, window: int = 2000) -> str | None:
    """按「带标签 > 中文年月日 > ISO > 斜杠 > 英文」的顺序找第一个合法日期。"""
    if not text:
        return None
    hay = text[:window]
    # 全角数字归一
    hay_norm = hay.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = _DATE_LABELED.search(hay_norm)
    if m:
        got = _mk(*m.groups())
        if got:
            return got
    for rx in (_DATE_CN, _DATE_DASH, _DATE_SLASH):
        m = rx.search(hay_norm)
        if m:
            got = _mk(*m.groups())
            if got:
                return got
    m = _DATE_EN.search(hay)
    if m:
        got = _mk(m.group(3), _MONTHS[m.group(2).lower().rstrip(".")], m.group(1))
        if got:
            return got
    m = _DATE_EN2.search(hay)
    if m:
        got = _mk(m.group(3), _MONTHS[m.group(1).lower().rstrip(".")], m.group(2))
        if got:
            return got
    return None


_EFFECTIVE = re.compile(
    r"(?:自|从)?\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?\s*"
    r"(?:起)?\s*(?:施行|生效|执行|实施|适用)"
)
_EFFECTIVE_EN = re.compile(
    r"(?:effective|enters? into force|applies from)\s*(?:on\s*)?"
    r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\.?\s*,?\s+(20\d{2})",
    re.I,
)


def parse_effective_date(text: str) -> str | None:
    if not text:
        return None
    m = _EFFECTIVE.search(text)
    if m:
        return _mk(*m.groups())
    m = _EFFECTIVE_EN.search(text[:4000])
    if m:
        return _mk(m.group(3), _MONTHS[m.group(2).lower().rstrip(".")], m.group(1))
    return None


# ------------------------------------------------------------------ 文号
_DOCNO_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 中文公文文号：国办发〔2025〕12号 / 沪府规〔2024〕3号 / 发改价格[2025]100号
    re.compile(
        r"[\u4e00-\u9fa5]{1,12}(?:发|办|函|公告|令|规|字|号)?\s*"
        r"[〔\[（(【]\s*20\d{2}\s*[〕\]）)】]\s*\d{1,4}\s*号"
    ),
    # 主席令/部令：第 12 号令、银保监会令2024年第3号
    re.compile(r"[\u4e00-\u9fa5]{2,20}令\s*(?:第\s*)?\d{1,4}\s*号"),
    re.compile(r"[\u4e00-\u9fa5]{2,20}(?:公告|通知)\s*(?:第\s*)?20\d{2}\s*年\s*第?\s*\d{1,4}\s*号"),
    # 金融监管总局式：金规〔2024〕5号 已覆盖；补充 "X监发〔...〕"
    # 国际：Federal Register 引证 89 FR 12345
    re.compile(r"\b\d{2,3}\s+FR\s+\d{3,6}\b"),
    # EU：Regulation (EU) 2024/1234、Directive (EU) 2023/45
    re.compile(r"\b(?:Regulation|Directive|Decision)\s*\((?:EU|EC|EEC)\)\s*\d{4}/\d{1,5}\b", re.I),
    re.compile(r"\b(?:EU|EC)\s*(?:No\.?)?\s*\d{4}/\d{1,5}\b"),
    # UK：SI 2024/1234
    re.compile(r"\bSI\s+\d{4}/\d{1,4}\b"),
)


def extract_docno(text: str, declared: str | None = None) -> str:
    """从正文里提取文号。优先取显式声明的字段值。"""
    if declared and declared.strip():
        return declared.strip()[:80]
    if not text:
        return ""
    head = text[:6000]
    for rx in _DOCNO_PATTERNS:
        m = rx.search(head)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return ""


_ISSUER_PATTERNS = (
    re.compile(r"(?:发布机关|发文机关|发布机构|来源)[：:]\s*([^\n]{2,40})"),
    re.compile(r"^\s*([\u4e00-\u9fa5]{4,30}(?:部|委|局|署|办公厅|人民政府|总局|银行|协会|委员会))\s*$", re.M),
)


def extract_issuer(text: str, declared: str = "") -> str:
    if declared:
        return declared.strip()[:60]
    if not text:
        return ""
    for rx in _ISSUER_PATTERNS:
        m = rx.search(text[:3000])
        if m:
            return re.sub(r"\s+", "", m.group(1))[:60]
    return ""


# ------------------------------------------------------------------ 完整性判定
def detect_blocked(html: str, text: str) -> str | None:
    """检测反爬/验证/登录页。返回原因或 None。"""
    hay = f"{html[:4000]}\n{text[:2000]}"
    for rx in _BLOCK_PATTERNS:
        m = rx.search(hay)
        if m:
            return f"命中阻塞特征: {m.group(0)[:40]}"
    return None


def looks_js_only(html: str, text: str) -> bool:
    """正文过短 + 命中 JS 骨架特征 → 判定需要渲染。"""
    if len(text) >= 200:
        return False
    for rx in _JS_ONLY_PATTERNS:
        if rx.search(html[:20000]):
            return True
    return False


def classify_status(
    html: str,
    text: str,
    min_chars: int = 200,
    max_chars: int = 0,
) -> tuple[str, str]:
    """返回 (FetchStatus 值, 说明)。这是四态判定的唯一入口。"""
    blocked = detect_blocked(html, text)
    if blocked:
        return FetchStatus.BLOCKED.value, blocked
    if looks_js_only(html, text):
        return FetchStatus.BLOCKED.value, "疑似 JS 渲染骨架，静态获取未得到正文"
    n = len(text or "")
    if n < min_chars:
        return FetchStatus.LISTING_ONLY.value, f"正文仅 {n} 字，低于阈值 {min_chars}"
    if looks_like_nav_page(text, min_chars):
        return FetchStatus.LISTING_ONLY.value, "疑似导航/镜像工具页而非正文"
    if max_chars and n >= max_chars:
        return FetchStatus.PARTIAL.value, f"正文达到截断上限 {max_chars} 字"
    return FetchStatus.FULL.value, ""
