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
from itertools import zip_longest
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
    if stype == "render":
        raise ValueError("该源列表需要渲染；请用网页工具读取或配置静态/API 列表适配器")

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
    if opts.max_fetch < 0 or (opts.max_fetch == 0 and not opts.listing_only):
        raise ValueError("正文采集 max_fetch 必须大于零；仅采列表请使用 --listing-only")
    for value in (opts.since, opts.until):
        if value:
            dt.date.fromisoformat(value)
    if opts.since and opts.until and opts.since > opts.until:
        raise ValueError("since 不得晚于 until")
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

    selected = registry.select(opts.sources)
    # Read before any requests or writes: a corrupt ledger must never be replaced.
    index = load_index(archive_root)
    contact = str(registry.meta.get("contact_ua") or "").strip()
    if contact and "请在此填写" not in contact:
        if not contact.isascii():
            raise ValueError("meta.contact_ua 必须为 ASCII HTTP 请求头")
        opts.net.user_agent = contact

    fetcher = Fetcher(opts.net)
    collected: list[Article] = []
    fetched_count = 0
    archive_path = ""
    dupes: list[dict[str, Any]] = []
    kept_articles: list[Article] = []
    batches: list[list[Article]] = []

    try:
        # ---------- 1/3 列表 + 初筛 ----------
        for source in selected:
            attempt_start = len(fetcher.attempts)
            try:
                items = collect_listing(source, fetcher, report)
            except Exception as exc:  # noqa: BLE001
                failures.append({"source": source.name, "stage": "listing",
                                 "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            listing_attempts = fetcher.attempts[attempt_start:]
            def request_identity(attempt):
                return attempt.request_id or attempt.url
            successful_requests = {request_identity(a) for a in listing_attempts if a.ok}
            failed = [a for a in listing_attempts if not a.ok and (
                request_identity(a) not in successful_requests or (a.error or "").startswith("JSON 解析失败")
            )]
            if failed:
                failures.append({"source": source.key, "stage": "listing",
                                 "error": "; ".join(a.error or "请求失败" for a in failed)[-500:]})
            elif not items:
                failures.append({"source": source.key, "stage": "listing",
                                 "error": "列表未解析出条目；无法区分空栏目与结构变化，请人工复核"})

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
            batches.append(kept)

        # ---------- 2/3 正文抓取 ----------
        if not opts.listing_only:
            budget = max(0, opts.max_fetch - fetched_count)
            # Share a bounded detail budget across sources instead of exhausting it
            # on the first listing. The caller's source order breaks ties.
            fetch_queue = (art for row in zip_longest(*batches) for art in row if art is not None)
            for art in fetch_queue:
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
        windowed = [art for art in collected if in_window(art, opts.since, opts.until)]
        report_window_dropped = len(collected) - len(windowed)
        kept_articles, dupes = dedupe(windowed)
        for source in selected:
            for status in FetchStatus:
                report.bump(source.key, status.value, 0)
                report.per_source[source.key][status.value] = 0
        for art in kept_articles:
            report.bump(art.source_key, art.fetch_status, 1)
        report.articles = kept_articles
        report.finished_at = dt.datetime.now().isoformat(timespec="seconds")

        entries = index.setdefault("entries", {})
        for art in kept_articles:
            previous = entries.get(art.url)
            first_seen = (previous or {}).get("first_seen") or str((previous or {}).get("fetched_at") or "")[:10]
            art.first_seen = first_seen or art.first_seen or today_str()
            art.fetched_at = report.finished_at
            # 一律落快照：即使没取得正文，也要留一条可审计记录（含 attempts 与失败原因），
            # 否则报告无法解释"哪些来源没拿到、为什么没拿到"。
            snap_path = snapshot_article(art, archive_root)
            row = index_entry(art, snap_path)
            row["first_seen"] = art.first_seen
            row["fetched_at"] = report.finished_at
            history = list((previous or {}).get("history") or [])
            if previous and previous.get("snapshot_path") != row["snapshot_path"]:
                history.append({k: previous.get(k) for k in (
                    "snapshot_id", "snapshot_path", "fetch_status", "content_hash", "fetched_at"
                )})
            row["history"] = history
            entries[art.url] = row
        index["meta"] = {
            "updated_at": report.finished_at,
            "total": len(entries),
            "full_text_rate": (sum(row.get("fetch_status") == "full" for row in entries.values()) / len(entries)) if entries else 0.0,
            "run_full_text_rate": report.full_text_rate,
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
        "window_dropped_after_fetch": report_window_dropped,
        "archive_path": archive_path,
        "ledger_path": os.path.join(archive_root, "index.json"),
        "coverage": {
            "sources": [s.key for s in selected],
            "regions": sorted({s.region for s in selected}),
            "layers": sorted({s.layer for s in selected}),
            "scope_complete": False,
            "note": "有限信源注册表不代表全球覆盖；零命中不得解释为没有政策动态。",
        },
    }
    return report, summary
