# -*- coding: utf-8 -*-
"""政策情报检索层（collector）

分层：
    net.py        HTTP 客户端（代理修复、限速、重试、尝试记录）
    registry.py   sources.yaml 声明式信源注册表
    listparse.py  列表解析（JSON / HTML / RSS / sitemap）
    extract.py    正文与元数据抽取 + 完整性四态判定
    pdfx.py       PDF 文字层解析（pypdf）
    fetch.py      正文回落链（api → text → html → pdf → render → 明确失败）
    store.py      快照 / 证据索引 / 内容级去重 / 全文存档
    verify.py     逐项声明与证据核验（旧全局检查为显式兼容模式）
    run.py        采集编排
    cli.py        命令行入口

对外最常用的三个入口：
    python -m collector doctor
    python -m collector collect --max-fetch 30
    python -m collector verify 报告.md --claims claims.json --evidence evidence
"""
from __future__ import annotations

__version__ = "1.1.0"

from .models import Article, FetchStatus, PolicyStatus, RunReport  # noqa: F401

__all__ = ["Article", "FetchStatus", "PolicyStatus", "RunReport", "__version__"]
