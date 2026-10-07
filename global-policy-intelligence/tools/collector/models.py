# -*- coding: utf-8 -*-
"""collector · 数据模型

核心是 FetchStatus 四态——它是整个防幻觉体系的开关：
结论强度上限由「正文拿到了多少」决定，而不是由模型自信程度决定。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

class FetchStatus(str, Enum):
    """正文完整性四态。任何进入分析的条目都必须带这个标记。"""

    FULL = "full"                    # 拿到完整正文
    PARTIAL = "partial"              # 拿到正文但被截断/缺字段
    LISTING_ONLY = "listing_only"    # 只有标题+链接（列表页级别）
    SCANNED_PDF = "scanned_pdf"      # PDF 无文字层，需 OCR/VLM
    BLOCKED = "blocked"              # 反爬/登录墙/渲染失败，明确未取得

    @property
    def allows_core_conclusion(self) -> bool:
        """该状态下的证据能否支撑核心结论（这是硬闸门）。"""
        return self in (FetchStatus.FULL,)

    @property
    def allows_any_conclusion(self) -> bool:
        return self in (FetchStatus.FULL, FetchStatus.PARTIAL)


class PolicyStatus(str, Enum):
    """法律/政策状态——必须与来源事实分开记录。"""

    EFFECTIVE = "已生效"
    PENDING = "已公布待生效"
    DRAFT = "草案/征求意见"
    GUIDANCE = "政策倡议/指导意见"
    ENFORCEMENT = "执法案例/监管信号"
    OPINION = "行业观点/舆情"
    UNKNOWN = "未确认"


@dataclass
class Article:
    """一条被采集到的政策条目。字段命名与 ledger 输出保持一致。"""

    url: str
    source: str
    source_key: str = ""
    region: str = "境内"
    title: str = ""
    date: str | None = None                 # 发布日期 YYYY-MM-DD
    effective_date: str | None = None       # 生效日期
    issuer: str = ""                        # 发布机关
    docno: str = ""                         # 文号
    policy_status: str = PolicyStatus.UNKNOWN.value
    text: str = ""
    summary: str = ""
    fetch_status: str = FetchStatus.LISTING_ONLY.value
    fetch_method: str = ""                  # api / feed / html / pdf / render / mirror
    attempts: list[dict[str, Any]] = field(default_factory=list)
    first_seen: str = ""
    evidence_layer: str = "L1"              # L1 原始权威 / L2 官方解释
    notes: str = ""
    # 列表记录的原始字段（用于正文接口模板渲染）。
    # 为什么需要它：正文接口常需要列表里才有的字段（docId、id、file_code…），
    # 而详情页 URL 里未必带这些参数——只从 URL 正则取 ID 会漏掉大量信源。
    params: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- 派生属性
    @property
    def text_chars(self) -> int:
        return len(self.text or "")

    @property
    def status_enum(self) -> FetchStatus:
        try:
            return FetchStatus(self.fetch_status)
        except ValueError:
            return FetchStatus.LISTING_ONLY

    @property
    def snapshot_id(self) -> str:
        """内容指纹：URL + 正文哈希。用于检测「同一链接内容变了」的版本漂移。"""
        payload = f"{self.url}\n{self.text or ''}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def head_tail(self, n: int = 200) -> tuple[str, str]:
        """首尾各 n 字的指纹——最短时间内判断「是不是抓到了正文」的实用技巧。"""
        body = re.sub(r"\s+", " ", self.text or "").strip()
        if len(body) <= 2 * n:
            return body, body
        return body[:n], body[-n:]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_ledger_row(self) -> dict[str, Any]:
        """证据台账 19 字段行（见 references/evidence-and-sources.md）。"""
        head, tail = self.head_tail()
        return {
            "source_key": self.source_key,
            "source": self.source,
            "layer": self.evidence_layer,
            "issuer": self.issuer,
            "title": self.title,
            "docno": self.docno or "无/未标明",
            "pub_date": self.date or "未标明",
            "effective_date": self.effective_date or "不适用",
            "policy_status": self.policy_status,
            "fetch_status": self.fetch_status,
            "fetch_method": self.fetch_method,
            "text_chars": self.text_chars,
            "snapshot_id": self.snapshot_id,
            "head_fingerprint": head,
            "tail_fingerprint": tail,
            "url": self.url,
            "attempts": [a.get("url") for a in self.attempts],
            "notes": self.notes,
        }


@dataclass
class RunReport:
    """一次采集运行的统计——用于计算「正文获取率」。"""

    started_at: str = ""
    finished_at: str = ""
    per_source: dict[str, dict[str, int]] = field(default_factory=dict)
    articles: list[Article] = field(default_factory=list)

    def bump(self, key: str, field_name: str, n: int = 1) -> None:
        row = self.per_source.setdefault(
            key, {"listed": 0, "kept": 0, "full": 0, "partial": 0, "blocked": 0, "listing_only": 0}
        )
        row[field_name] = row.get(field_name, 0) + n

    @property
    def full_text_rate(self) -> float:
        """正文获取率 = full / 全部条目。这是优化检索策略的主指标。"""
        total = len(self.articles)
        if not total:
            return 0.0
        full = sum(1 for a in self.articles if a.fetch_status == FetchStatus.FULL.value)
        return full / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "full_text_rate": round(self.full_text_rate, 4),
            "total_articles": len(self.articles),
            "per_source": self.per_source,
        }
