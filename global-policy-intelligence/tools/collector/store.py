# -*- coding: utf-8 -*-
"""collector · 存证层（快照 / 索引 / 去重）

这是把「来源台账」从元数据升级为**可审计证据**的关键：
台账里记的是"我说我读过"，快照里存的是"我读到的是这些字"。

同时提供内容级去重——URL 级去重解决不了「同一条政策被五个站转载」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict
from typing import Any, Iterable

from .models import Article, FetchStatus

INDEX_NAME = "index.json"


# ------------------------------------------------------------------ 归一化
def fold_for_match(text: str) -> str:
    """用于引文比对的归一化：全角转半角、去空白、去零宽、统一引号。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\s\u3000]+", "", text)
    return text


def title_key(title: str) -> str:
    """标题指纹：只保留中英文与数字，用于识别同题转载。"""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title).lower()
    text = re.sub(r"[^\w\u4e00-\u9fa5]+", "", text)
    return text


def content_hash(text: str) -> str:
    return hashlib.sha256(fold_for_match(text).encode("utf-8")).hexdigest()[:16]


def sha256_16(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()[:16]


# ------------------------------------------------------------------ 快照
def snapshot_article(article: Article, root: str) -> str:
    """落盘一篇正文快照，返回相对路径。"""
    day = article.date or article.first_seen or "unknown-date"
    safe_source = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", article.source_key or article.source)[:40]
    rel_dir = os.path.join("snapshots", day[:4], day[:7], safe_source)
    abs_dir = os.path.join(root, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)

    name = f"{article.snapshot_id}.json"
    rel_path = os.path.join(rel_dir, name)
    abs_path = os.path.join(root, rel_path)

    head, tail = article.head_tail()
    payload = article.to_dict()
    payload.update(
        {
            "snapshot_id": article.snapshot_id,
            "head_fingerprint": head,
            "tail_fingerprint": tail,
            "archived_at": article.first_seen,
        }
    )
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return rel_path.replace("\\", "/")


# ------------------------------------------------------------------ 索引
def load_index(root: str) -> dict[str, Any]:
    path = os.path.join(root, INDEX_NAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"entries": {}}
    return {"entries": {}}


def save_index(root: str, index: dict[str, Any]) -> str:
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, INDEX_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    return path


def index_entry(article: Article, snapshot_path: str) -> dict[str, Any]:
    row = article.to_ledger_row()
    row.update(
        {
            "snapshot_path": snapshot_path,
            "snapshot_id": article.snapshot_id,
            "content_hash": content_hash(article.text),
            "title_key": title_key(article.title),
            "fetched_at": article.first_seen,
        }
    )
    return row


# ------------------------------------------------------------------ 去重
def dedupe(articles: Iterable[Article]) -> tuple[list[Article], list[dict[str, Any]]]:
    """返回 (保留条目, 重复说明)。

    三级判定：
      ① URL 完全相同
      ② 正文内容哈希相同（跨站转载同一条政策）
      ③ 归一化标题相同 + 同月（防止同一文件的多语/多版重复计数）
    保留优先级：正文更完整 > 权威层级更高（L1 > L2）> 先到者
    """
    kept: dict[str, Article] = {}
    dupes: list[dict[str, Any]] = []
    by_url: dict[str, str] = {}
    by_text: dict[str, str] = {}
    by_title: dict[str, str] = {}

    layer_rank = {"L1": 0, "L2": 1}

    def score(a: Article) -> tuple[int, int]:
        return (
            0 if a.fetch_status == FetchStatus.FULL.value else 1,
            layer_rank.get(a.evidence_layer, 9),
        )

    for art in articles:
        ck = content_hash(art.text) if art.text else ""
        tk = f"{title_key(art.title)}|{(art.date or '')[:7]}"

        existing_key: str | None = None
        reason = ""
        if art.url in by_url:
            existing_key, reason = by_url[art.url], "URL 相同"
        elif ck and ck in by_text:
            existing_key, reason = by_text[ck], "正文内容哈希相同（跨站转载）"
        elif tk.strip("|") and tk in by_title:
            existing_key, reason = by_title[tk], "标题指纹与月份相同"

        if existing_key is None:
            key = art.url or art.snapshot_id
            kept[key] = art
            if art.url:
                by_url[art.url] = key
            if ck:
                by_text[ck] = key
            if tk.strip("|"):
                by_title[tk] = key
            continue

        current = kept.get(existing_key)
        if current is not None and score(art) < score(current):
            # 新条目更完整/更权威 → 替换，旧条目降为重复
            kept[existing_key] = art
            dupes.append(
                {
                    "dropped_url": current.url,
                    "kept_url": art.url,
                    "reason": f"{reason}；保留更完整/更权威的一条",
                }
            )
        else:
            dupes.append(
                {
                    "dropped_url": art.url,
                    "kept_url": current.url if current else existing_key,
                    "reason": reason,
                }
            )
    return list(kept.values()), dupes


def write_archive(articles: list[Article], root: str, label: str) -> str:
    """生成人类可读的全文存档（便于人工复核与二次检索）。"""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{label}_全文存档.md")
    lines = [
        f"# 政策全文存档 · {label}",
        "",
        "> 本文件保存本次采集到的公开正文全文，供核验、复核与二次检索使用。",
        "> 每条都带 fetch_status 与快照 ID；未取得正文的条目不会被假装成正文。",
        "",
    ]
    for n, art in enumerate(articles, 1):
        lines.append(f"## {n}. {art.title}")
        lines.append(f"- 来源：{art.source}（{art.evidence_layer}） · {art.date or '日期未标明'}")
        if art.docno:
            lines.append(f"- 文号：{art.docno}")
        lines.append(f"- 原文链接：{art.url}")
        lines.append(f"- fetch_status：{art.fetch_status}（{art.fetch_method}）")
        lines.append(f"- 快照：{art.snapshot_id}")
        if art.text:
            lines.append("")
            lines.append(art.text)
        else:
            lines.append("")
            lines.append(f"**未取得正文**：{art.notes or '未说明'}")
            lines.append("")
            lines.append(f"已尝试：{json.dumps(art.attempts, ensure_ascii=False)}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def load_snapshot(root: str, snapshot_path: str) -> dict[str, Any] | None:
    # 空路径必须显式挡掉：否则 os.path.join(root, "") 会退化成根目录本身，
    # 打开目录会抛 FileNotFoundError / IsADirectoryError（实测踩到）。
    if not snapshot_path:
        return None
    abs_path = os.path.join(root, snapshot_path)
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return None


def all_snapshot_texts(root: str) -> dict[str, str]:
    """把 index.json 指向的所有快照正文读进内存：{url: text}。"""
    index = load_index(root)
    out: dict[str, str] = {}
    for url, row in (index.get("entries") or {}).items():
        snap = load_snapshot(root, row.get("snapshot_path", ""))
        if snap:
            out[url] = snap.get("text") or ""
    return out


def row_to_article(row: dict[str, Any]) -> Article:
    fields = {k: v for k, v in row.items() if k in Article.__dataclass_fields__}
    return Article(**fields)  # type: ignore[arg-type]


def article_asdict(article: Article) -> dict[str, Any]:
    return asdict(article)
