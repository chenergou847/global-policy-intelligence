# -*- coding: utf-8 -*-
"""collector · 分页目标展开（纯标准库，零内部依赖）

为什么单独成一个模块：这段逻辑原先写在两处——listparse.build_page_targets（真抓取用）
和 dryrun（只打印计划用）。两份实现迟早不同步，而 dry-run 与实际请求不一致会直接
误导用户（"计划里说会请求 A，实际请求了 B"）。

这里只依赖 registry 与标准库，因此 dry-run 也能在缺 httpx/lxml 时使用。
"""
from __future__ import annotations

import json
from typing import Any

from .registry import Source


def expand(text: str, page: int, rows_per_page: int) -> str:
    """把 {page} / {start} 占位替换成第 page 页的取值。"""
    return (
        str(text)
        .replace("{page}", str(page))
        .replace("{start}", str((page - 1) * max(1, rows_per_page)))
    )


def page_targets(source: Source) -> list[tuple[str, Any]]:
    """按 pages 展开 (url, json_body) 列表；body 为 None 表示 GET。"""
    listing = source.listing
    rows = max(1, listing.rows_per_page)
    out: list[tuple[str, Any]] = []
    for page in range(1, max(1, listing.pages) + 1):
        url = expand(listing.url, page, rows)
        body: Any = None
        if listing.body:
            try:
                body = json.loads(expand(listing.body, page, rows))
            except json.JSONDecodeError:
                body = None
        out.append((url, body))
    return out
