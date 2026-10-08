# -*- coding: utf-8 -*-
"""collector · 采集编排

一次运行 = 列表 → 初筛 → 正文抓取 → 去重 → 存证 → 台账。

关键工程纪律（每条都对应一个真实踩过的坑）：
  · 单个信源失败不影响整体，但**必须显式记录**失败原因
  · 每抓一条正文前先查缓存（同一次运行内不重复请求同一 URL）
  · 正文抓取有总数上限，避免一次跑掉几千个请求
  · 只保留窗口内条目；无日期的条目按"日期未标明"保留但降级提示
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

from . import listparse
from .fetch import fetch_article
from .models import Article, FetchStatus, RunReport
from .net import Fetcher, NetConfig
from .registry import Registry, Source, load_registry
from .store import (
    content_hash,
    dedupe,
    index_entry,
    load_index,
    save_index,
    snapshot_article,
    title_key,
    write_archive,
)


@dataclass
class CollectOptions:
    sources: list[str] = field(default_factory=list)      # 空 = 全部启用
    since: str | None = None                              # YYYY-MM-DD 窗口起点
    until: str | None = None                              # YYYY-MM-DD 窗口终点
    max_fetch: int = 40                                   # 本次最多抓多少篇正文
    listing_only: bool = False                            # 只采集列表，不抓正文
    dry_run: bool = False                                 # 只打印将要请求的地址，不发任何请求
    net: NetConfig = field(default_factory=NetConfig)
    archive_root: str = ""


def today_str() -> str:
    return dt.date.today().isoformat()


def in_window(article: Article, since: str | None, until: str | None) -> bool:
    if not article.date:
        return True          # 无日期不丢，靠 fetch_status 与备注提示
    if since and article.date < since:
        return False
    if until and article.date > until:
        return False
    return True


# ------------------------------------------------------------------ 列表阶段
def collect_listing(
    source: Source, fetcher: Fetcher, report: RunReport
) -> list[Article]:
    articles: list[Article] = []
    stype = source.type

    if stype in ("json_get", "json_api", "post_json", "shanghai_policy"):
        for url, body in listparse.build_page_targets(source):
            if body is not None:
                payload = fetcher.request(url, tier="api", method="POST", json_body=body)
                payload = payload.json() if payload is not None and payload.status_code == 200 else None
            else:
                payload = fetcher.get_json(url, tier="api")
            if payload is None:
                continue
            articles.extend(
                listparse.parse_json_records(payload, source.extract, source, url)
            )
    elif stype in ("rss", "atom", "feed"):
        xml_text = fetcher.get_text(source.listing.url, tier="feed", encoding=source.listing.encoding)
        if xml_text:
            articles.extend(listparse.parse_feed(xml_text, source, source.listing.url))
    elif stype == "sitemap":
        since = None
        if getattr(source, "days", None):
            since = (dt.date.today() - dt.timedelta(days=int(source.days))).isoformat()
        xml_text = fetcher.get_text(source.listing.url, tier="feed", encoding=source.listing.encoding)
        if xml_text:
            articles.extend(
                listparse.parse_sitemap(xml_text, source, source.listing.url, since)
            )
    else:  # html_list 及未知类型
        for url, _ in listparse.build_page_targets(source):
            html = fetcher.get_text(url, tier="html", encoding=source.listing.encoding)
            if not html:
                continue
            articles.extend(
                listparse.parse_html_listing(html, source.extract, source, url)
            )
            if not articles:
                break     # 首页就解析不到，后面页大概率一样

    report.bump(source.key, "listed", len(articles))
    return articles


# ------------------------------------------------------------------ 主流程
def run_collect(
    registry_path: str,
    archive_root: str,
    options: CollectOptions | None = None,
) -> tuple[RunReport, dict[str, Any]]:
    opts = options or CollectOptions()
    report = RunReport(started_at=dt.datetime.now().isoformat(timespec="seconds"))

    # dry-run 走独立模块（只依赖 pyyaml，不碰 httpx/lxml）
    if opts.dry_run:
        from .dryrun import build_dry_run_plan

        report.finished_at = dt.datetime.now().isoformat(timespec="seconds")
        return report, build_dry_run_plan(
            registry_path,
            sources=opts.sources,
            max_fetch=opts.max_fetch,
            listing_only=opts.listing_only,
        )

    registry: Registry = load_registry(registry_path)
    failures: list[dict[str, str]] = []

    selected = [
        s for s in registry.enabled
        if not opts.sources or s.key in opts.sources
    ]

    fetcher = Fetcher(opts.net)
    collected: list[Article] = []
    fetched_count = 0
    archive_path = ""
    dupes: list[dict[str, Any]] = []
    kept_articles: list[Article] = []

    try:
        # ---------- 1/3 列表 + 初筛 ----------
        for source in selected:
            try:
                items = collect_listing(source, fetcher, report)
            except Exception as exc:  # noqa: BLE001
                failures.append({"source": source.name, "stage": "listing",
                                 "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue

            kept: list[Article] = []
            seen_urls: set[str] = set()
            for art in items:
                if art.url in seen_urls:
                    continue
                if not in_window(art, opts.since, opts.until):
                    continue
                if not source.passes_keywords(art.title, art.summary):
                    continue
                seen_urls.add(art.url)
                kept.append(art)
            kept = kept[: source.max_items]
            report.bump(source.key, "kept", len(kept))
            collected.extend(kept)

        # ---------- 2/3 正文抓取 ----------
        if not opts.listing_only:
            budget = max(0, opts.max_fetch - fetched_count)
            for art in collected:
                if budget <= 0:
                    break
                source = registry.by_key(art.source_key)
                if source is None:
                    continue
                if art.fetch_status != FetchStatus.LISTING_ONLY.value:
                    continue
                try:
                    fetch_article(source, art, fetcher)
                except Exception as exc:  # noqa: BLE001
                    art.fetch_status = FetchStatus.BLOCKED.value
                    art.notes = f"抓取异常: {type(exc).__name__}: {exc}"[:200]
                report.bump(source.key, art.fetch_status, 1)
                fetched_count += 1
                budget -= 1

        # ---------- 3/3 去重 + 存证 ----------
        kept_articles, dupes = dedupe(collected)
        report.articles = kept_articles
        report.finished_at = dt.datetime.now().isoformat(timespec="seconds")

        index = load_index(archive_root)
        entries = index.setdefault("entries", {})
        for art in kept_articles:
            if not art.first_seen:
                art.first_seen = today_str()
            # 一律落快照：即使没取得正文，也要留一条可审计记录（含 attempts 与失败原因），
            # 否则报告无法解释"哪些来源没拿到、为什么没拿到"。
            snap_path = snapshot_article(art, archive_root)
            entries[art.url] = index_entry(art, snap_path)
        index["meta"] = {
            "updated_at": report.finished_at,
            "total": len(entries),
            "full_text_rate": report.full_text_rate,
        }
        save_index(archive_root, index)

        label = today_str()
        archive_path = write_archive(kept_articles, archive_root, label)

    finally:
        fetcher.close()

    summary: dict[str, Any] = {
        "run": report.to_dict(),
        "failures": failures,
        "duplicates": dupes,
        "archive_path": archive_path,
        "ledger_path": os.path.join(archive_root, "index.json"),
    }
    return report, summary


def default_archive_root(collector_dir: str) -> str:
    """默认证据库位置：<skill>/evidence"""
    tools_dir = os.path.dirname(collector_dir)
    skill_dir = os.path.dirname(tools_dir)
    return os.path.join(skill_dir, "evidence")
