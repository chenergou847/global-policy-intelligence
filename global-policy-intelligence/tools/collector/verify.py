# -*- coding: utf-8 -*-
"""collector · 幻觉校验器（verify）

严格模式通过 schema_version=1 的 claims ledger 限定逐项引用快照。
只核验 fact 文本中的引文、文号、日期、数字；核心 fact/inference 的
每条依据必须有可读 full L1/L2 正文。旧 API 保留全局兼容检查并明确降级。

机器不能判断所有实质正文是否已覆盖核心声明，也不能判断事实语义、
推论是否成立或公司自有数据是否真实；报告验收仍需人工复核。
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# 刻意不导入 .extract：那个模块 import lxml，而本模块（幻觉校验）实际只用
# 标准库 + .store，全部能力都不需要 HTML 解析。这里曾残留一行
# `from . import extract as ex`（从未被调用），却让"只想校验报告"的用户
# 也被迫装 lxml。已删除——校验器现在是零外部依赖的。
from .models import Article, FetchStatus
from .store import fold_for_match
from .claims import Claim, load_claims

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

_CLAIM_QUOTE = re.compile(r'「([^」]+)」|“([^”]+)”|"([^"\n]+)"')
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")
_CLAIM_AMOUNT = re.compile(
    r"(?<![\d.])\d+(?:\.\d+)?(?:%|千万元|百万元|亿元|万元|美元|欧元|元|"
    r"million|billion|bn|mn|USD|EUR|RMB|CNY)"
    r"|(?:USD|EUR|RMB|CNY|[$€£¥])\d+(?:\.\d+)?(?![\d.])",
    re.I,
)
_MANUAL_BOUNDARY = (
    "机器仅核验 ledger 中声明的证据关联与文本命中；不能判断所有实质正文是否已覆盖"
    "核心声明，也不能判断事实语义、推论成立或公司自有数据真实性，须人工复核。"
)


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
    audit_mode: str = "legacy"

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def strict_passed(self) -> bool:
        """Whether ledger machine checks passed, not whether the report was accepted."""
        return self.audit_mode == "strict" and self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "audit_mode": self.audit_mode,
            "strict_passed": self.strict_passed,
            "manual_review_required": True,
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


# ------------------------------------------------------------------ 逐项审计
def _claim_fold(text: str) -> str:
    return canon_numbers(canon_dates(fold_for_match(text)))


def _claim_quotes(text: str) -> list[str]:
    quotes = [next(part for part in match.groups() if part is not None)
              for match in _CLAIM_QUOTE.finditer(text)]
    block: list[str] = []
    for line in [*text.splitlines(), ""]:
        match = re.match(r"^\s*>\s?(.*)$", line)
        if match and match.group(1).strip():
            block.append(match.group(1))
        elif block:
            quotes.append("\n".join(block))
            block = []
    return quotes


def _check_report_binding(report: str, claim: Claim, result: VerifyResult) -> None:
    """Check direct/reference-style citations in the claim's Markdown paragraph.

    One claim per paragraph keeps the association explicit. A separate source
    appendix can enumerate ledger URLs; prose semantics still need review.
    """
    if claim.kind not in ("fact", "inference") or not claim.evidence:
        return
    cited = {match.group(0).rstrip(".,;") for match in _URL_IN_MD.finditer(report)}
    if cited and any(url not in cited for url in claim.evidence):
        result.findings.append(Finding(
            "C5-声明覆盖", "error", "台账依据未出现在报告实际引用中", claim.id,
        ))
    references = {}
    for line in report.splitlines():
        match = re.match(r"^\s*\[([^\]]+)\]:\s*(?:<)?(https?://[^\s>]+)", line)
        if match:
            references[match.group(1).casefold()] = match.group(2)
    for block in re.split(r"\n\s*\n", report):
        if fold_for_match(claim.text) not in fold_for_match(block):
            continue
        actual = {match.group(0).rstrip(".,;") for match in _URL_IN_MD.finditer(block)}
        for label in re.findall(r"\[([^\]]+)\]", block):
            if label.casefold() in references:
                actual.add(references[label.casefold()])
        mismatched = actual - set(claim.evidence)
        if mismatched:
            result.findings.append(Finding(
                "C5-声明覆盖", "error", "该主张正文段落的引用与台账 evidence 不一致",
                f"{claim.id}: " + "; ".join(sorted(mismatched)),
            ))


def _check_fact(claim: Claim, texts: dict[str, str], report_quotes: list[str], result: VerifyResult) -> None:
    # Keep snapshots separate: an exact quotation cannot straddle two documents.
    quote_corpus = [fold_for_match(canon_dates(text)) for text in texts.values()]
    corpus = [_claim_fold(text) for text in texts.values()]
    quotes = _claim_quotes(claim.text)
    claim_folded = fold_for_match(claim.text)
    quotes.extend(quote for quote in report_quotes if fold_for_match(quote) in claim_folded)
    quotes = list(dict.fromkeys(quotes))
    result.stats["quotes_total"] += len(quotes)
    for quote in quotes:
        needle = fold_for_match(canon_dates(quote))
        if any(needle in text for text in quote_corpus):
            result.stats["quotes_verified"] += 1
        else:
            result.findings.append(Finding(
                "C2-逐字引文", "error", "引文未命中该项指定的引用快照正文",
                f"{claim.id}: {quote}",
            ))

    raw_text = unicodedata.normalize("NFKC", _URL_IN_MD.sub("", claim.text))
    text = _claim_fold(raw_text)
    dates = [match.group(0) for match in _DATE_ANY.finditer(text)]
    docnos = [_claim_fold(match.group(0)) for match in _DOCNO.finditer(raw_text)]
    amounts = [match.group(0) for match in _CLAIM_AMOUNT.finditer(text)]
    numbers = [match.group(0) for match in _NUMBER.finditer(_DATE_ANY.sub("", text))]
    for label, tokens in (("文号", docnos), ("完整日期", dates),
                          ("金额/门槛", amounts), ("数字", numbers)):
        for token in dict.fromkeys(tokens):
            if label == "文号":
                token = re.split(r"根据|依据|按照|参照|文号为|文号是|文号", token)[-1]
            pattern = re.compile(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", re.I)
            if any(pattern.search(source) for source in corpus):
                continue
            result.findings.append(Finding(
                "C3-文号数字", "error", f"{label}未命中该项指定的引用快照正文",
                f"{claim.id}: {token}",
            ))


def check_claims(
    report: str, archive_root: str, index: dict[str, Any],
    claims: list[Claim], result: VerifyResult,
) -> None:
    from .store import content_hash, load_snapshot

    entries = index.get("entries") or {}
    counts: dict[str, int] = {}
    for row in entries.values():
        status = str(row.get("fetch_status")) if isinstance(row, dict) else "invalid"
        counts[status] = counts.get(status, 0) + 1
    result.stats.update({
        "claims_total": len(claims), "claims_verified": 0,
        "core_claims": sum(claim.requires_full_evidence for claim in claims),
        "quotes_total": 0, "quotes_verified": 0, "evidence_counts": counts,
        "full_text_rate": round(counts.get("full", 0) / len(entries), 4) if entries else 0.0,
    })
    report_folded = fold_for_match(report)
    report_quotes = _claim_quotes(report)
    cache: dict[str, dict[str, Any] | None] = {}
    for claim in claims:
        errors_before = len(result.errors)
        _check_report_binding(report, claim, result)
        if fold_for_match(claim.text) not in report_folded:
            result.findings.append(Finding(
                "C5-声明覆盖", "error", "claims.text 未在报告中可核对出现", claim.id,
            ))
        if not claim.evidence and claim.kind in ("fact", "inference"):
            result.findings.append(Finding(
                "C4-证据强度", "error" if claim.requires_full_evidence else "warn",
                "声明未指定证据 URL，无法进行来源审计", claim.id,
            ))
        texts: dict[str, str] = {}
        for url in claim.evidence:
            row = entries.get(url)
            detail = f"{claim.id}: {url}"
            if not isinstance(row, dict):
                result.findings.append(Finding(
                    "C1-引用链接", "error", "声明指定 URL 不在证据索引中", detail,
                ))
                continue
            if url not in cache:
                snapshot = None
                path = row.get("snapshot_path")
                if isinstance(path, str) and path:
                    try:
                        snapshot = load_snapshot(archive_root, path)
                    except (OSError, UnicodeError, ValueError):
                        pass
                cache[url] = snapshot if isinstance(snapshot, dict) else None
            snapshot = cache[url]
            if snapshot is None:
                result.findings.append(Finding(
                    "C4-证据强度", "error", "声明指定 URL 缺少可读取的库内 JSON 快照", detail,
                ))
                continue
            if snapshot.get("url") != url or row.get("url", url) != url:
                result.findings.append(Finding(
                    "C1-引用链接", "error", "快照或索引行 URL 与声明引用不一致", detail,
                ))
                continue
            text = snapshot.get("text")
            if not isinstance(text, str):
                result.findings.append(Finding(
                    "C4-证据强度", "error", "快照正文不是字符串", detail,
                ))
                continue
            actual_id = Article(url=url, source="", text=text).snapshot_id
            readable = bool(fold_for_match(text))
            hashes = {content_hash(text)}
            if not readable:
                hashes.add("")  # Empty bodies in new indexes have no content hash.
            if (not isinstance(row.get("content_hash"), str)
                    or row.get("content_hash") not in hashes
                    or row.get("snapshot_id") != actual_id
                    or snapshot.get("snapshot_id") != actual_id):
                result.findings.append(Finding(
                    "C4-证据强度", "error", "索引/快照指纹与实际正文不一致或缺失", detail,
                ))
                continue
            full = (row.get("fetch_status") == FetchStatus.FULL.value
                    and snapshot.get("fetch_status") == FetchStatus.FULL.value)
            authoritative = (row.get("layer") in ("L1", "L2")
                             and snapshot.get("evidence_layer") in ("L1", "L2"))
            if claim.requires_full_evidence and not (readable and full and authoritative):
                result.findings.append(Finding(
                    "C4-证据强度", "error", "核心 fact/inference 必须由可读 full L1/L2 快照支撑",
                    detail,
                ))
            elif not (readable and full and authoritative):
                result.findings.append(Finding(
                    "C4-证据强度", "warn", "辅助证据未达到可读 full L1/L2，不得支撑核心结论",
                    detail,
                ))
            if readable:
                texts[url] = text
        if claim.kind == "fact":
            _check_fact(claim, texts, report_quotes, result)
        if len(result.errors) == errors_before and (claim.evidence or claim.kind in ("company", "assumption")):
            result.stats["claims_verified"] += 1


# ------------------------------------------------------------------ 对外接口
def verify_report(
    report_text: str,
    archive_root: str,
    *,
    require_all_citations: bool = True,
    claims: dict[str, Any] | str | os.PathLike[str] | None = None,
    strict: bool | None = None,
) -> VerifyResult:
    """Claims enable strict auditing; a historical call without claims stays compatible.

    Pass strict=True to require a ledger, including when claims is missing. The CLI
    uses this default; strict=False is reserved for explicit legacy checks.
    """
    from .store import all_snapshot_texts, load_index

    use_strict = claims is not None if strict is None else strict
    result = VerifyResult(audit_mode="strict" if use_strict else "legacy")
    result.stats["audit_mode"] = result.audit_mode
    try:
        index = load_index(archive_root)
        if not isinstance(index, dict) or not isinstance(index.get("entries", {}), dict):
            raise ValueError("index.entries must be an object")
    except (OSError, UnicodeError, ValueError) as exc:
        index = {"entries": {}}
        result.findings.append(Finding("C4-证据强度", "error", "证据索引不可读取", str(exc)))

    check_citations(report_text, index, result)
    if use_strict:
        result.findings.append(Finding("审计边界", "warn", _MANUAL_BOUNDARY))
        if claims is None:
            result.findings.append(Finding(
                "C0-声明台账", "error", "严格模式缺少 claims ledger；请使用 --claims claims.json",
            ))
            return result
        try:
            ledger = load_claims(claims)
        except ValueError as exc:
            result.findings.append(Finding("C0-声明台账", "error", "claims ledger 无效", str(exc)))
            return result
        check_claims(report_text, archive_root, index, ledger, result)
        return result

    result.findings.append(Finding(
        "审计模式", "warn",
        "兼容检查未进行逐项审计，不能声称报告通过严格验收；请提供 --claims claims.json。",
    ))
    if claims is not None:
        result.findings.append(Finding(
            "C0-声明台账", "error", "claims ledger 不得在兼容模式中被忽略",
        ))
    try:
        snapshots = all_snapshot_texts(archive_root)
    except (OSError, UnicodeError, ValueError) as exc:
        result.findings.append(Finding("C4-证据强度", "error", "证据快照不可读取", str(exc)))
        return result
    check_quotes(report_text, snapshots, result)
    check_numbers(report_text, snapshots, result)
    check_evidence_strength(index, result)

    if not require_all_citations:
        for f in result.findings:
            if f.check == "C1-引用链接":
                f.level = "warn"
    return result


def main(argv: list[str] | None = None) -> int:
    from .console import configure_output

    configure_output()
    import argparse

    ap = argparse.ArgumentParser(
        prog="verify",
        description="逐项证据审计（默认严格模式，需 --claims；旧检查需显式 --legacy-checks-only）",
    )
    ap.add_argument("report", help="报告 Markdown 路径")
    ap.add_argument("--evidence", required=True, help="证据库根目录（含 index.json 与 snapshots/）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--claims", help="schema_version=1 的 JSON claims ledger 路径")
    mode.add_argument("--legacy-checks-only", action="store_true", help="仅执行旧全局检查，无逐项审计")
    ap.add_argument("--allow-unknown-citations", action="store_true",
                    help="仅在旧模式中将未收录链接降级为警告")
    args = ap.parse_args(argv)

    try:
        with open(args.report, encoding="utf-8") as f:
            report_text = f.read()
    except (OSError, UnicodeError) as exc:
        ap.error(f"无法读取报告：{exc}")

    result = verify_report(
        report_text, args.evidence, require_all_citations=not args.allow_unknown_citations,
        claims=args.claims, strict=not args.legacy_checks_only,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        label = "逐项证据校验" if result.audit_mode == "strict" else "幻觉校验（旧兼容检查，无逐项审计）"
        print(f"{label}：{'通过' if result.ok else '未通过'}")
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
