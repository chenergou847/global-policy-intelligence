# -*- coding: utf-8 -*-
"""collector · 存证层（快照 / 索引 / 去重）

这是把「来源台账」从元数据升级为**可审计证据**的关键：
台账里记的是"我说我读过"，快照里存的是"我读到的是这些字"。

同时提供内容级去重——URL 级去重解决不了「同一条政策被五个站转载」。
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import tempfile
import unicodedata
import uuid
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


# ------------------------------------------------------------------ 快照
def snapshot_article(article: Article, root: str) -> str:
    """落盘一篇正文快照，返回相对路径。"""
    day = article.date or article.first_seen or "unknown-date"
    safe_source = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", article.source_key or article.source)[:40]
    rel_dir = os.path.join("snapshots", day[:4], day[:7], safe_source)
    abs_dir = os.path.join(root, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)

    # Retrieval status and document metadata can change even when the body does
    # not. Keep those versions distinct without changing the content fingerprint.
    metadata = {key: getattr(article, key) for key in (
        "title", "date", "effective_date", "issuer", "docno", "policy_status",
        "fetch_status", "fetch_method", "evidence_layer", "notes",
    )}
    version = hashlib.sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    # Every acquisition is immutable, including partial -> full -> partial.
    name = f"{article.snapshot_id}_{version}_{uuid.uuid4().hex}.json"
    rel_path = os.path.join(rel_dir, name)
    abs_path = os.path.join(root, rel_path)

    head, tail = article.head_tail()
    payload = article.to_dict()
    payload.update(
        {
            "snapshot_id": article.snapshot_id,
            "head_fingerprint": head,
            "tail_fingerprint": tail,
            "archived_at": article.fetched_at or article.first_seen,
        }
    )
    with open(abs_path, "x", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return rel_path.replace("\\", "/")


# ------------------------------------------------------------------ 索引
def _validate_index(index: Any) -> dict[str, Any]:
    if not isinstance(index, dict) or not isinstance(index.get("entries"), dict):
        raise ValueError("Invalid evidence index: expected an object with an entries object")
    if any(not isinstance(row, dict) for row in index["entries"].values()):
        raise ValueError("Invalid evidence index: each entry must be an object")
    return index


def load_index(root: str) -> dict[str, Any]:
    path = os.path.join(root, INDEX_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            index = json.load(f)
    except FileNotFoundError:
        return {"entries": {}}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid evidence index at {path}: {exc}") from exc
    return _validate_index(index)


def save_index(root: str, index: dict[str, Any]) -> str:
    _validate_index(index)
    # Never replace a damaged historical index with a fresh collection.
    load_index(root)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, INDEX_NAME)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root,
            prefix=f".{INDEX_NAME}.", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(index, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return path


def index_entry(article: Article, snapshot_path: str) -> dict[str, Any]:
    row = article.to_ledger_row()
    row.update(
        {
            "snapshot_path": snapshot_path,
            "snapshot_id": article.snapshot_id,
            "content_hash": content_hash(article.text) if fold_for_match(article.text) else "",
            "title_key": title_key(article.title),
            "fetched_at": article.fetched_at or article.first_seen,
        }
    )
    return row


# ------------------------------------------------------------------ 去重
def dedupe(articles: Iterable[Article]) -> tuple[list[Article], list[dict[str, Any]]]:
    """返回 (保留条目, 重复说明)。

    三级判定：
      ① URL 完全相同
      ② 正文内容哈希相同（跨站转载同一条政策）
      ③ 同一明确文号 + 发布机关
    保留优先级：正文更完整 > 权威层级更高（L1 > L2）> 先到者
    """
    kept: dict[int, Article] = {}
    dupes: list[dict[str, Any]] = []
    dupe_groups: list[int] = []
    by_url: dict[str, int] = {}
    by_text: dict[str, int] = {}
    by_doc: dict[tuple[str, str], int] = {}
    parents: dict[int, int] = {}
    arrival: dict[int, int] = {}

    unknown = {
        "", "无", "无文号", "未标明", "无/未标明", "未确认", "未知",
        "不适用", "未提供", "待确认", "none", "null", "unknown", "n/a", "na", "-", "--",
    }

    def document_key(a: Article) -> tuple[str, str] | None:
        docno = fold_for_match(a.docno).casefold()
        issuer = fold_for_match(a.issuer).casefold()
        if docno in unknown or issuer in unknown:
            return None
        return docno, issuer

    def group_for(key: int) -> int:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    layer_rank = {"L1": 0, "L2": 1}

    def score(a: Article) -> tuple[int, int]:
        return (
            0 if a.fetch_status == FetchStatus.FULL.value else 1,
            layer_rank.get(a.evidence_layer, 9),
        )

    for position, art in enumerate(articles):
        ck = content_hash(art.text) if fold_for_match(art.text) else ""
        dk = document_key(art)
        matches: dict[int, str] = {}
        for alias, lookup, reason in (
            (art.url, by_url, "URL 相同"),
            (ck, by_text, "正文内容哈希相同（跨站转载）"),
            (dk, by_doc, "明确文号与发布机关相同"),
        ):
            if alias and alias in lookup:
                matches.setdefault(group_for(lookup[alias]), reason)

        if not matches:
            key = position
            parents[key] = key
            kept[key] = art
            arrival[key] = position
        else:
            key = min(matches)
            best_key = min(matches, key=lambda k: (score(kept[k]), arrival[k]))
            current = kept[best_key]
            replace = score(art) < score(current)
            winner = art if replace else current
            winner_arrival = position if replace else arrival[best_key]

            for matched_key, reason in matches.items():
                if replace or matched_key != best_key:
                    dupes.append({
                        "dropped_url": kept[matched_key].url,
                        "kept_url": winner.url,
                        "reason": f"{reason}；保留更完整/更权威的一条",
                    })
                    dupe_groups.append(key)
            if not replace:
                dupes.append({
                    "dropped_url": art.url,
                    "kept_url": winner.url,
                    "reason": matches[best_key],
                })
                dupe_groups.append(key)

            # A new article can join several groups through different aliases.
            # Redirect all old aliases and duplicate reports to the final winner.
            for matched_key in matches:
                if matched_key != key:
                    parents[matched_key] = key
                    del kept[matched_key]
                    del arrival[matched_key]
            kept[key] = winner
            arrival[key] = winner_arrival

        for alias, lookup in (
            (art.url, by_url), (ck, by_text), (dk, by_doc),
        ):
            if alias:
                lookup[alias] = key

    for row, key in zip(dupes, dupe_groups):
        row["kept_url"] = kept[group_for(key)].url
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
    if not isinstance(snapshot_path, str):
        return None
    # Check Windows path syntax even when evidence is read on another platform.
    if os.path.isabs(snapshot_path) or ntpath.isabs(snapshot_path) or ntpath.splitdrive(snapshot_path)[0]:
        return None
    relative = snapshot_path.replace("\\", "/")
    if ".." in relative.split("/"):
        return None
    abs_root = os.path.realpath(root)
    abs_path = os.path.realpath(os.path.join(abs_root, relative))
    try:
        if os.path.normcase(os.path.commonpath([abs_root, abs_path])) != os.path.normcase(abs_root):
            return None
    except ValueError:
        return None
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, encoding="utf-8") as f:
        try:
            snapshot = json.load(f)
            return snapshot if isinstance(snapshot, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
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
