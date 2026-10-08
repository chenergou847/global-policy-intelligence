# -*- coding: utf-8 -*-
"""collector · dry-run 计划

独立成模块的原因（实测教训）：
本函数只需要 PyYAML（读 sources.yaml），却在最初实现在 run.py 里，
而 run.py 顶部 import 了 httpx/lxml → 缺依赖的用户跑 `collect --dry-run`
照样抛 ImportError。偏偏这个命令正是"缺依赖时最该先跑"的那一个。

因此这里刻意**只依赖 registry**（registry 只依赖 pyyaml），
不 import httpx / lxml / fetch / store 中的任何一个。
"""
from __future__ import annotations

from typing import Any

from .registry import load_registry


def build_dry_run_plan(
    registry_path: str,
    *,
    sources: list[str] | None = None,
    max_fetch: int = 40,
    listing_only: bool = False,
) -> dict[str, Any]:
    """生成 dry-run 计划：只读配置，不发任何请求。"""
    registry = load_registry(registry_path)
    wanted = [s for s in sources or [] if s]
    selected = [s for s in registry.enabled if not wanted or s.key in wanted]

    plan: list[dict[str, Any]] = []
    for source in selected:
        listing = source.listing
        urls: list[str] = []
        for page in range(1, max(1, listing.pages) + 1):
            urls.append(
                listing.url.replace("{page}", str(page)).replace(
                    "{start}", str((page - 1) * max(1, listing.rows_per_page))
                )
            )
        plan.append({
            "key": source.key,
            "name": source.name,
            "probe": source.probe,
            "type": source.type,
            "listing_requests": urls,
            "detail_mode": source.detail.mode,
            "detail_tiers": source.detail.resolved_tiers(),
            "declared_max_items": source.max_items,
        })

    return {
        "dry_run": True,
        "failures": [],          # 与正常路径保持同一 schema，便于调用方统一处理
        "duplicates": [],
        "archive_path": "",
        "ledger_path": "",
        "note": ("未发送任何请求。确认无误后去掉 --dry-run 再执行；"
                 "并建议先去掉 sources.yaml 中 meta.contact_ua 的占位邮箱。"),
        "sources_selected": len(plan),
        "listing_requests_would_send": sum(len(p["listing_requests"]) for p in plan),
        "detail_fetch_budget": 0 if listing_only else max_fetch,
        "plan": plan,
    }
