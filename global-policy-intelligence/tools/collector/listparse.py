# -*- coding: utf-8 -*-
"""collector · 列表页解析

同一个「列表」在现实里有六种形态：JSON 接口、HTML 列表、RSS、Atom、sitemap、
POST JSON。这里把它们统一解析成 Article 列表（此时正文还是空的，
fetch_status 先记为 listing_only，等抓正文后再升级）。

字段映射全部来自信源配置，不硬编码任何站点的字段名。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import lxml.etree as ET
import lxml.html as LH

from . import extract as ex
from .models import Article, FetchStatus
from .registry import ExtractSpec, Source, _deep_get, _pick

# ------------------------------------------------------------------ 链接处理
_TRACKING = re.compile(r"([?&])(utm_[^&#]*|spm=[^&#]*|from=[^&#]*|share_[^&#]*)=?[^&#]*")


def clean_url(url: str) -> str:
    if not url:
        return ""
    if "utm_" in url or "spm=" in url:
        url = _TRACKING.sub(lambda m: m.group(1), url)
        url = re.sub(r"[?&]+$", "", url).replace("?&", "?").replace("&&", "&")
    return url.strip()


def absolutize(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return f"{urlsplit(base).scheme}:{href}"
    return urljoin(base, href)


# ------------------------------------------------------------------ URL 模板
class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # 缺字段时留空而不是抛异常
        return ""


def fill_template(template: str, record: dict[str, Any]) -> str:
    flat = {
        k: (v if isinstance(v, (str, int, float)) else "")
        for k, v in (record or {}).items()
    }
    try:
        return template.format_map(_SafeDict(flat))
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 日期归一
_EPOCH_MS = 10**12


def normalize_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > _EPOCH_MS:      # 毫秒
            ts /= 1000.0
        if ts > 10**9:
            return time.strftime("%Y-%m-%d", time.localtime(ts))
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{10,13}", text):
        return normalize_date(int(text))
    return ex.parse_any_date(text, window=len(text) + 1)


def date_from_url(url: str, pattern: str | None, fmt: str | None = None) -> str | None:
    if not pattern:
        return None
    m = re.search(pattern, url)
    if not m:
        return None
    groups = m.groups()
    if len(groups) >= 3:
        return ex._mk(groups[0], groups[1], groups[2])
    if len(groups) == 1:
        raw = groups[0]
        if re.fullmatch(r"\d{8}", raw):
            return ex._mk(raw[:4], raw[4:6], raw[6:8])
    return None


# ------------------------------------------------------------------ JSON 列表
def parse_json_records(
    payload: Any, spec: ExtractSpec, source: Source, base_url: str
) -> list[Article]:
    rows = _deep_get(payload, spec.records_path)
    if rows is None:
        # 兜底：在顶层找第一个「看起来是记录数组」的字段
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    rows = value
                    break
        elif isinstance(payload, list):
            rows = payload
    if not isinstance(rows, list):
        return []

    out: list[Article] = []
    for record in rows:
        if not isinstance(record, dict):
            continue
        title = str(_pick(record, spec.title_field) or "").strip().replace("\n", " ")
        if len(title) < 6:
            continue
        if spec.link_template:
            link = fill_template(spec.link_template, record)
        else:
            link = str(_pick(record, spec.link_field) or "")
            if link and spec.link_prefix and not link.startswith("http"):
                link = spec.link_prefix + link
        link = clean_url(absolutize(base_url, link))
        if not link:
            continue
        out.append(
            Article(
                url=link,
                source=source.name,
                source_key=source.key,
                region=source.region,
                title=title[:300],
                date=normalize_date(_pick(record, spec.date_field)),
                docno=str(_pick(record, spec.docno_field) or "")[:80],
                summary=str(_pick(record, spec.summary_field) or "")[:400],
                fetch_status=FetchStatus.LISTING_ONLY.value,
                fetch_method="api",
                evidence_layer=source.layer,
                notes=source.notes,
                params={
                    k: v
                    for k, v in record.items()
                    if isinstance(v, (str, int, float))
                },
            )
        )
    return out


# ------------------------------------------------------------------ HTML 列表
def parse_html_listing(
    html: str, spec: ExtractSpec, source: Source, base_url: str
) -> list[Article]:
    tree = ex.parse_html(html)
    if tree is None:
        return []
    pattern = re.compile(spec.link_pattern) if spec.link_pattern else None

    anchors: list[LH.HtmlElement]
    if spec.html_item_selector:
        anchors = ex.select_nodes(tree, spec.html_item_selector)
    else:
        anchors = list(tree.xpath("//a[@href]"))

    out: list[Article] = []
    seen: set[str] = set()
    for node in anchors:
        href = node.get("href") if hasattr(node, "get") else None
        if href is None and node.tag == "a":
            href = node.get("href")
        if not href:
            # 选择器命中的可能是 li/div，往下找第一个 a
            found = node.xpath(".//a[@href]") if hasattr(node, "xpath") else []
            if not found:
                continue
            node, href = found[0], found[0].get("href")
        if pattern and not pattern.search(href or ""):
            continue
        title = ex.normalize_text(node.text_content() if hasattr(node, "text_content") else "")
        title = title.replace("\n", " ").strip()
        if len(title) < 8 or title.endswith(">>"):
            continue
        url = clean_url(absolutize(base_url, href))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            Article(
                url=url,
                source=source.name,
                source_key=source.key,
                region=source.region,
                title=title[:300],
                date=date_from_url(url, spec.date_from_url, spec.date_format),
                fetch_status=FetchStatus.LISTING_ONLY.value,
                fetch_method="html",
                evidence_layer=source.layer,
                notes=source.notes,
            )
        )
    return out


# ------------------------------------------------------------------ RSS / Atom
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str, source: Source, base_url: str) -> list[Article]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        try:
            parser = ET.XMLParser(recover=True, encoding="utf-8")
            root = ET.fromstring(xml_text.encode("utf-8", errors="replace"), parser=parser)
        except Exception:  # noqa: BLE001
            return []

    entries = [el for el in root.iter() if _localname(el.tag) in ("item", "entry")]
    out: list[Article] = []
    for entry in entries:
        title = link = summary = date_raw = ""
        for child in entry:
            name = _localname(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = child.get("href") or (child.text or "").strip() or link
            elif name in ("description", "summary", "encoded"):
                summary = re.sub(r"<[^>]+>", "", "".join(child.itertext())).strip()
            elif name in ("pubdate", "published", "updated", "date", "lastmod"):
                if not date_raw:
                    date_raw = (child.text or "").strip()
        if len(title) < 8 or not link:
            continue
        out.append(
            Article(
                url=clean_url(absolutize(base_url, link)),
                source=source.name,
                source_key=source.key,
                region=source.region,
                title=title[:300],
                date=normalize_date(date_raw),
                summary=summary[:400],
                fetch_status=FetchStatus.LISTING_ONLY.value,
                fetch_method="feed",
                evidence_layer=source.layer,
                notes=source.notes,
            )
        )
    return out


# ------------------------------------------------------------------ sitemap
def parse_sitemap(
    xml_text: str, source: Source, base_url: str, since: str | None = None
) -> list[Article]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    pattern = re.compile(source.extract.link_pattern) if source.extract.link_pattern else None
    out: list[Article] = []
    for block in root.iter():
        if _localname(block.tag) != "url":
            continue
        loc = mod = ""
        for child in block:
            name = _localname(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                mod = (child.text or "").strip()
        if not loc:
            continue
        if pattern and not pattern.search(loc):
            continue
        date = normalize_date(mod)
        if since and date and date < since:
            continue
        slug = urlsplit(loc).path.rstrip("/").split("/")[-1].replace("-", " ").strip()
        out.append(
            Article(
                url=loc,
                source=source.name,
                source_key=source.key,
                region=source.region,
                title=(slug or loc)[:300],
                date=date,
                fetch_status=FetchStatus.LISTING_ONLY.value,
                fetch_method="sitemap",
                evidence_layer=source.layer,
                notes="标题为 URL slug 占位，待正文抓取替换",
            )
        )
    out.sort(key=lambda a: a.date or "", reverse=True)
    return out


# ------------------------------------------------------------------ 分页参数
def build_page_targets(source: Source) -> list[tuple[str, Any]]:
    """按 pages 展开 (url, json_body) 列表。支持 {page} / {start} 占位。"""
    listing = source.listing
    targets: list[tuple[str, Any]] = []
    for page in range(1, max(1, listing.pages) + 1):
        url = listing.url.replace("{page}", str(page)).replace(
            "{start}", str((page - 1) * max(1, listing.rows_per_page))
        )
        body = None
        if listing.body:
            try:
                body = json.loads(
                    listing.body.replace("{page}", str(page)).replace(
                        "{start}", str((page - 1) * max(1, listing.rows_per_page))
                    )
                )
            except json.JSONDecodeError:
                body = None
        targets.append((url, body))
    return targets
