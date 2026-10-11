# -*- coding: utf-8 -*-
"""collector · 信源注册表

把「检索」从临场搜索变成声明式配置：每个信源声明
列表怎么取、字段怎么映射、正文走哪条回落链、失败算什么。

这就是 skill 里原先缺失的那一层——规范说了「要打开原文」，
但没有说清「从哪个 URL、用哪种方式、失败了怎么办」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_DETAIL_TIERS = ("api", "text", "html", "pdf")


@dataclass
class Listing:
    url: str = ""
    method: str = "GET"
    body: str | None = None
    pages: int = 1
    rows_per_page: int = 20
    quirks: str = ""
    encoding: str | None = None


@dataclass
class ExtractSpec:
    records_path: str | None = None
    title_field: str | None = None
    date_field: str | None = None
    docno_field: str | None = None
    link_field: str | None = None
    link_template: str | None = None
    link_prefix: str | None = None
    summary_field: str | None = None
    html_item_selector: str | None = None
    link_pattern: str | None = None
    date_from_url: str | None = None
    date_format: str | None = None


@dataclass
class Detail:
    mode: str = "html"                     # json_api / html / pdf / text / render / blocked
    api_url: str | None = None
    body_field: str | None = None
    content_selectors: list[str] = field(default_factory=list)
    pdf_field: str | None = None
    pdf_selectors: list[str] = field(default_factory=list)
    text_url_template: str | None = None   # 纯文本正文地址模板（如 FR 的 raw_text_url）
    text_api_template: str | None = None   # 可选：由接口返回 raw_text_url 字段，比拼接更可靠
    blocked_reason: str | None = None
    tiers: list[str] = field(default_factory=list)

    def resolved_tiers(self) -> list[str]:
        return list(self.tiers) if self.tiers else list(DEFAULT_DETAIL_TIERS)


@dataclass
class Source:
    key: str
    name: str
    region: str = "境内"
    layer: str = "L1"
    enabled: bool = True
    type: str = "html_list"
    listing: Listing = field(default_factory=Listing)
    extract: ExtractSpec = field(default_factory=ExtractSpec)
    detail: Detail = field(default_factory=Detail)
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    max_items: int = 20
    days: int = 0
    min_body_chars: int = 200
    max_body_chars: int = 0
    allow_insecure_tls: bool = False
    notes: str = ""
    human_assist: bool = False
    probe: str = "unknown"        # verified / partial / unverified —— 该信源是否实测可用

    # ---------------------------------------------------------------- 过滤
    def passes_keywords(self, title: str, summary: str = "") -> bool:
        hay = f"{title} {summary}".lower()
        if self.exclude_keywords and any(k.lower() in hay for k in self.exclude_keywords):
            return False
        if not self.keywords:
            return True
        return any(k.lower() in hay for k in self.keywords)


@dataclass
class Registry:
    sources: list[Source]
    meta: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    def by_key(self, key: str) -> Source | None:
        for s in self.sources:
            if s.key == key:
                return s
        return None

    @property
    def enabled(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def select(self, keys: list[str] | None = None) -> list[Source]:
        """Explicit keys opt in even when disabled; defaults contain official sources only."""
        if keys:
            wanted = list(dict.fromkeys(keys))
            unknown = [key for key in wanted if self.by_key(key) is None]
            if unknown:
                raise ValueError("未知信源 key：" + ", ".join(unknown))
            selected = [self.by_key(key) for key in wanted]
        else:
            selected = [s for s in self.enabled if s.layer in ("L1", "L2")]
        if not selected:
            raise ValueError("没有可采集的信源；请用 --sources 明确指定 key，或启用 L1/L2 信源")
        return selected


# ------------------------------------------------------------------ 加载
def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _deep_get(record: Any, path: str | None) -> Any:
    """按 'response.docs' 或 'data.list' 取值；也支持列表本身。"""
    if not path:
        return record
    cur = record
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _pick(record: dict[str, Any], spec: str | None) -> Any:
    """字段名支持 'a|b|c' 回落链与点号路径。"""
    if not spec:
        return None
    for name in str(spec).split("|"):
        name = name.strip()
        if not name:
            continue
        value = _deep_get(record, name)
        if value not in (None, "", [], {}):
            if isinstance(value, list):
                return value[0] if value else None
            return value
    return None


def load_registry(path: str) -> Registry:
    if not os.path.exists(path):
        raise FileNotFoundError(f"信源配置不存在: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"无效 YAML 信源配置：{exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("信源配置必须包含 sources 数组")
    if raw.get("meta") is not None and not isinstance(raw["meta"], dict):
        raise ValueError("meta 必须为对象")

    sources: list[Source] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw.get("sources") or []):
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"].strip():
            raise ValueError(f"sources[{idx}] 缺少有效 key")
        key = item["key"]
        if key in seen:
            raise ValueError(f"重复信源 key：{key}")
        seen.add(key)
        for flag in ("enabled", "allow_insecure_tls", "human_assist"):
            if flag in item and not isinstance(item[flag], bool):
                raise ValueError(f"{key}.{flag} 必须为 YAML 布尔值")
        if item.get("layer", "L1") not in ("L1", "L2", "L3", "L4", "L5"):
            raise ValueError(f"{key}.layer 必须为 L1/L2/L3/L4/L5")
        if item.get("type", "html_list") not in (
            "json_get", "json_api", "post_json", "shanghai_policy", "html_list", "rss", "atom", "feed", "sitemap", "render"
        ):
            raise ValueError(f"{key}.type 不受支持")
        for section in ("listing", "extract", "detail"):
            if item.get(section) is not None and not isinstance(item[section], dict):
                raise ValueError(f"{key}.{section} 必须为对象")
        listing_cfg = item.get("listing") or {}
        extract_cfg = item.get("extract") or {}
        detail_cfg = item.get("detail") or {}
        if extract_cfg.get("html") is not None and not isinstance(extract_cfg["html"], dict):
            raise ValueError(f"{key}.extract.html 必须为对象")
        html_cfg = extract_cfg.get("html") or {}
        sources.append(
            Source(
                key=str(item.get("key") or f"src{idx}"),
                name=str(item.get("name") or item.get("key") or f"src{idx}"),
                region=str(item.get("region") or "境内"),
                layer=str(item.get("layer") or "L1"),
                enabled=bool(item.get("enabled", True)),
                type=str(item.get("type") or "html_list"),
                listing=Listing(
                    url=str(listing_cfg.get("url") or ""),
                    method=str(listing_cfg.get("method") or "GET").upper(),
                    body=listing_cfg.get("body"),
                    pages=int(listing_cfg.get("pages") or 1),
                    rows_per_page=int(listing_cfg.get("rows_per_page") or 20),
                    quirks=str(listing_cfg.get("quirks") or ""),
                    encoding=listing_cfg.get("encoding"),
                ),
                extract=ExtractSpec(
                    records_path=extract_cfg.get("records_path"),
                    title_field=extract_cfg.get("title_field"),
                    date_field=extract_cfg.get("date_field"),
                    docno_field=extract_cfg.get("docno_field"),
                    link_field=extract_cfg.get("link_field"),
                    link_template=extract_cfg.get("link_template"),
                    link_prefix=extract_cfg.get("link_prefix"),
                    summary_field=extract_cfg.get("summary_field"),
                    html_item_selector=html_cfg.get("item_selector"),
                    link_pattern=html_cfg.get("link_pattern"),
                    date_from_url=html_cfg.get("date_from_url"),
                    date_format=html_cfg.get("date_format"),
                ),
                detail=Detail(
                    mode=str(detail_cfg.get("mode") or "html"),
                    api_url=detail_cfg.get("api_url"),
                    body_field=detail_cfg.get("body_field"),
                    content_selectors=_as_str_list(detail_cfg.get("content_selectors")),
                    pdf_field=detail_cfg.get("pdf_field"),
                    pdf_selectors=_as_str_list(detail_cfg.get("pdf_selectors")),
                    text_url_template=detail_cfg.get("text_url_template"),
                    text_api_template=detail_cfg.get("text_api_template"),
                    blocked_reason=detail_cfg.get("blocked_reason"),
                    tiers=_as_str_list(detail_cfg.get("tiers")),
                ),
                keywords=_as_str_list(item.get("keywords")),
                exclude_keywords=_as_str_list(item.get("exclude_keywords")),
                max_items=int(item.get("max_items") or 20),
                days=int(item.get("days") or 0),
                min_body_chars=int(item.get("min_body_chars") or 200),
                max_body_chars=int(item.get("max_body_chars") or 0),
                allow_insecure_tls=bool(item.get("allow_insecure_tls", False)),
                notes=str(item.get("notes") or ""),
                human_assist=bool(item.get("human_assist", False)),
                probe=str(item.get("probe") or "unknown"),
            )
        )
        source = sources[-1]
        if not source.listing.url.startswith(("http://", "https://")):
            raise ValueError(f"{key}.listing.url 必须为 HTTP(S) 地址")
        if source.listing.pages < 1 or source.listing.rows_per_page < 1 or source.max_items < 1:
            raise ValueError(f"{key} 页数、每页数量和 max_items 必须大于零")
        if source.min_body_chars < 1 or source.max_body_chars < 0 or source.days < 0:
            raise ValueError(f"{key} 正文字数或 days 无效")
        if any(t not in ("api", "text", "html", "pdf", "render") for t in source.detail.resolved_tiers()):
            raise ValueError(f"{key}.detail.tiers 包含未知抓取级")
    return Registry(sources=sources, meta=raw.get("meta") or {}, path=path)
