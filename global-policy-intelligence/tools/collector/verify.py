# -*- coding: utf-8 -*-
"""collector · 幻觉校验器（verify）

这是把「防幻觉」从自觉要求变成**可执行闸门**的部分。四条确定性检查：

  C1 引用链接：报告里每个来源 URL 必须在证据索引里真实存在
                → 消灭「看起来很像真的、但从未访问过」的链接
  C2 逐字引文：报告中 `> ` 引用块必须能在对应快照正文里逐字命中
                → 消灭「改写式引用」和凭记忆编造的条款
  C3 文号/数字：报告里的文号与金额必须能在快照里找到
                → 消灭编造文号、金额、门槛
  C4 证据强度：核心结论必须由 fetch_status=full 的正文支撑
                → 消灭「只有标题却写出结论」

诚实边界：本校验器**不能**判断结论方向（利好/利空）是否正确。
它只保证「你说的每个可核验事实都真的在证据里」。这一边界必须写进报告。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import extract as ex
from .models import FetchStatus
from .store import fold_for_match
from urllib.parse import urlsplit

# ------------------------------------------------------------------ 正则
_URL_IN_MD = re.compile(r"https?://[^\s\)\]\"'<>，。；）】]+")
_QUOTE_LINE = re.compile(r"^\s*(?:>\s?|「|“)(.{8,})$")
_DOCNO = re.compile(
    r"[\u4e00-\u9fa5]{1,12}(?:发|办|函|公告|令|规|字|号)?\s*"
    r"[〔\[（(【]\s*20\d{2}\s*[〕\]）)】]\s*\d{1,4}\s*号"
    r"|\b\d{2,3}\s+FR\s+\d{3,6}\b"
    r"|\b(?:Regulation|Directive)\s*\((?:EU|EC)\)\s*\d{4}/\d{1,5}\b"
    r"|\bSI\s+\d{4}/\d{1,4}\b",
    re.I,
)
# 金额/比例/门槛：只在**带货币或百分比单位**，或**有明确限定词**时才算需要核验的事实。
# 刻意不收「N年/N天」——政策文号里的 2026年、期限里的 3年 都不是金额，
# 收进来只会制造误报（实测误报 '2026年'）。
_AMOUNT = re.compile(
    r"\d[\d,\.]*\s*(?:%|％|亿元|万元|千万元|百万元|元|美元|欧元|"
    r"million|billion|bn|mn|USD|EUR|RMB|CNY)"
    r"|(?:不超过|不高于|不低于|达到|上限为|下限为|金额为|补贴|罚款|罚款金额|注册资本|"
    r"保费|资金支持|预算|限额)[^。；\n]{0,12}?\d[\d,\.]*\s*(?:亿元|万元|元|%|％)",
    re.I,
)
# 引号内的内容（中文引号/英文引号），长度 ≥8 视为逐字引文
_INLINE_QUOTE = re.compile(r"[「“\"]([^」”\"]{8,300})[」”\"]")

# 访问日期/检索日期是"我什么时候看的"，不是文件的发布或生效日期，
# 因此不应参与 C3 的日期核验（原文档里当然找不到它）。
_ACCESS_DATE = re.compile(
    r"(?:访问日期|检索日期|抓取日期|检查日期|获取日期|accessed|retrieved)[：:\s]*"
    r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})",
    re.I,
)

# 日期归一：`2026年11月1日` / `2026/11/01` / `2026-11-1` → 统一成 `2026-11-01`
_DATE_ANY = re.compile(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})\s*日?")


def canon_dates(text: str) -> str:
    """把任意写法的日期统一成 YYYY-MM-DD。

    这一步是必需的：报告里写 `2026-11-01`，原始政策原文写 `2026年11月1日`，
    不做归一会产生大量误报（实测踩到），而误报会让校验器失去可信度。
    """
    if not text:
        return ""

    def _sub(m: re.Match[str]) -> str:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        return f"{y}-{mo:02d}-{d:02d}"

    return _DATE_ANY.sub(_sub, text)


def canon_numbers(text: str) -> str:
    """数字归一：去掉千分位逗号、全角转半角，便于跨写法比对。"""
    return re.sub(r"(?<=\d),(?=\d)", "", text)


@dataclass
class Finding:
    check: str
    level: str            # error / warn
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "level": self.level, "message": self.message, "detail": self.detail}


@dataclass
class VerifyResult:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [f.to_dict() for f in self.errors],
            "warnings": [f.to_dict() for f in self.warnings],
            "stats": self.stats,
        }


# ------------------------------------------------------------------ C1 引用链接
def check_citations(report: str, index: dict[str, Any], result: VerifyResult) -> set[str]:
    entries = (index.get("entries") or {})
    known: set[str] = set(entries.keys())
    # 已知站点的裸域名：允许作为"来源与访问"这类出处说明出现，不当作引用链接。
    # 否则 `来源：https://www.federalregister.gov/ （访问日期…）` 会被误判为编造链接。
    known_domains = {urlsplit(u).netloc.lower() for u in known if u.startswith("http")}
    cited = {m.group(0).rstrip(".,;") for m in _URL_IN_MD.finditer(report)}
    unknown = []
    for u in sorted(cited):
        if u in known:
            continue
        parts = urlsplit(u)
        if parts.netloc.lower() in known_domains and parts.path in ("", "/"):
            continue
        unknown.append(u)
    for url in unknown:
        result.findings.append(
            Finding(
                check="C1-引用链接",
                level="error",
                message="报告中出现未在证据索引中的链接",
                detail=url,
            )
        )
    result.stats["cited_urls"] = len(cited)
    result.stats["known_urls"] = len(known)
    result.stats["unknown_urls"] = len(unknown)
    return cited


# ------------------------------------------------------------------ C2 逐字引文
def check_quotes(report: str, snapshots: dict[str, str], result: VerifyResult) -> None:
    """抓取报告里的引文（> 行 与 「」/“” 内联引文），在全部快照正文里找逐字命中。"""
    folded_corpus = {url: fold_for_match(canon_dates(text)) for url, text in snapshots.items()}
    all_folded = "\n".join(folded_corpus.values())

    quotes: list[str] = []
    for line in report.splitlines():
        m = _QUOTE_LINE.match(line)
        if m:
            quotes.append(m.group(1).strip())
    for m in _INLINE_QUOTE.finditer(report):
        quotes.append(m.group(1).strip())

    verified = 0
    for quote in quotes:
        if quote.startswith(("本文件", "模型", "说明", "口径", "流程", "全部来自", "本报告")):
            continue     # 报告自身的说明性引用块，不是来源引文
        needle = fold_for_match(canon_dates(quote))
        if len(needle) < 8:
            continue
        if needle in all_folded:
            verified += 1
        else:
            result.findings.append(
                Finding(
                    check="C2-逐字引文",
                    level="error",
                    message="引文无法在任何快照正文中逐字命中（疑似改写或凭记忆引用）",
                    detail=quote[:120],
                )
            )
    result.stats["quotes_total"] = len(quotes)
    result.stats["quotes_verified"] = verified


# ------------------------------------------------------------------ C3 文号与数字
def check_numbers(report: str, snapshots: dict[str, str], result: VerifyResult) -> None:
    """报告里的文号与带单位数字，必须能在某份快照里找到。

    注意日期歧义：裸数字太容易误报，所以这里只校验
      ① 文号（结构强，误报率低）
      ② 带单位/百分号的金额与门槛
      ③ 显式写出的 YYYY-MM-DD 完整日期
    并且先做日期与数字的**写法归一**，否则 `2026年11月1日` 与 `2026-11-01`
    会被判成对不上（实测误报）。
    """
    corpus_raw = canon_numbers(canon_dates("\n".join(snapshots.values())))
    corpus = fold_for_match(corpus_raw)

    # 访问日期豁免：它不是被引用文件的内容，原文里当然找不到
    exempt_dates = set()
    for m in _ACCESS_DATE.finditer(report):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        exempt_dates.add(f"{y}-{mo:02d}-{d:02d}")

    for label, matches in (
        ("文号", _DOCNO.findall(report)),
        ("金额/门槛", _AMOUNT.findall(report)),
        ("完整日期", re.findall(r"20\d{2}-\d{2}-\d{2}", report)),
    ):
        for raw in matches:
            token = raw if isinstance(raw, str) else "".join(raw)
            token = token.strip()
            if not token:
                continue
            if label == "完整日期" and token in exempt_dates:
                continue
            token_norm = canon_numbers(canon_dates(token))
            if token_norm in corpus or fold_for_match(token_norm) in corpus:
                continue
            result.findings.append(
                Finding(
                    check="C3-文号数字",
                    level="error",
                    message=f"{label}在证据快照中找不到出处",
                    detail=token,
                )
            )


# ------------------------------------------------------------------ C4 证据强度
def check_evidence_strength(index: dict[str, Any], result: VerifyResult) -> None:
    entries = list((index.get("entries") or {}).values())
    if not entries:
        result.findings.append(
            Finding(check="C4-证据强度", level="error", message="证据索引为空，无法支撑任何结论")
        )
        return
    counts: dict[str, int] = {}
    for row in entries:
        counts[str(row.get("fetch_status"))] = counts.get(str(row.get("fetch_status")), 0) + 1
    total = len(entries)
    full = counts.get(FetchStatus.FULL.value, 0)
    result.stats["evidence_counts"] = counts
    result.stats["full_text_rate"] = round(full / total, 4)

    weak = [
        row for row in entries
        if str(row.get("fetch_status")) in (FetchStatus.BLOCKED.value, FetchStatus.SCANNED_PDF.value)
    ]
    if weak:
        result.findings.append(
            Finding(
                check="C4-证据强度",
                level="warn",
                message=f"{len(weak)} 条证据未取得正文，不得支撑核心结论",
                detail="; ".join(f"{r.get('source')}:{r.get('url')}" for r in weak[:5]),
            )
        )
    if full == 0:
        result.findings.append(
            Finding(
                check="C4-证据强度",
                level="error",
                message="没有任何一条证据达到 full 状态，本报告不得给出高置信度结论",
            )
        )


# ------------------------------------------------------------------ 对外接口
def verify_report(
    report_text: str,
    archive_root: str,
    *,
    require_all_citations: bool = True,
) -> VerifyResult:
    from .store import all_snapshot_texts, load_index

    result = VerifyResult()
    index = load_index(archive_root)
    snapshots = all_snapshot_texts(archive_root)

    check_citations(report_text, index, result)
    check_quotes(report_text, snapshots, result)
    check_numbers(report_text, snapshots, result)
    check_evidence_strength(index, result)

    if not require_all_citations:
        for f in result.findings:
            if f.check == "C1-引用链接":
                f.level = "warn"
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="verify",
        description="对报告做确定性幻觉校验（C1 引用链接 / C2 逐字引文 / C3 文号数字 / C4 证据强度）",
    )
    ap.add_argument("report", help="报告 Markdown 路径")
    ap.add_argument("--evidence", required=True, help="证据库根目录（含 index.json 与 snapshots/）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--allow-unknown-citations", action="store_true",
                    help="未收录链接降级为警告而非错误")
    args = ap.parse_args(argv)

    with open(args.report, encoding="utf-8") as f:
        report_text = f.read()

    result = verify_report(
        report_text, args.evidence, require_all_citations=not args.allow_unknown_citations
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"幻觉校验：{'通过' if result.ok else '未通过'}")
        print(f"统计：{json.dumps(result.stats, ensure_ascii=False)}")
        for f in result.errors:
            print(f"  [错误] {f.check} | {f.message} | {f.detail[:100]}")
        for f in result.warnings:
            print(f"  [警告] {f.check} | {f.message} | {f.detail[:100]}")
        if result.ok and not result.warnings:
            print("  未发现问题")
    return 0 if result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
