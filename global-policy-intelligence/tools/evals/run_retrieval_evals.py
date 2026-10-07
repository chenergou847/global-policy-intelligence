# -*- coding: utf-8 -*-
"""检索策略评测（离线、可重复、无外部网络）

    python tools/evals/run_retrieval_evals.py

它做四件事：
  1. 校验 evals/evals.json 的结构与题目完整性
  2. 用本地静态服务器供应一批「故意难抓」的 fixtures，把完整采集流水线跑一遍
     ——  覆盖 full / partial / listing_only / scanned_pdf / blocked 五种结果
  3. 计算两个主指标：正文获取率、引用可核验率
  4. 跑幻觉回归：合格报告必须通过 C1–C4；编造报告必须被拦住

为什么要用本地 fixtures 而不是线上站点：评测必须**稳定可重复**。
线上站点会变、会被限流，用它做评测只会得到"今天恰好通过"。
线上验证是另一件事（见 tools/tests/ 与手工 doctor）。
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import shutil
import socketserver
import sys
import threading

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
SKILL = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)

from collector import verify as vf                      # noqa: E402
from collector.models import FetchStatus                # noqa: E402
from collector.net import NetConfig, prepare_proxy_env   # noqa: E402
from collector.run import CollectOptions, run_collect    # noqa: E402
from collector.store import load_index                   # noqa: E402

EVALS_JSON = os.path.join(SKILL, "evals", "evals.json")
SITE = os.path.join(HERE, "site")
ARCHIVE = os.path.join(HERE, ".evidence")
CONFIG = os.path.join(HERE, "sources.eval.yaml")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(f"{name}{'' if ok else ' :: ' + detail}")


# ============================================================ 1. 题目结构校验
def check_eval_spec() -> None:
    with open(EVALS_JSON, encoding="utf-8") as f:
        spec = json.load(f)
    evals = spec.get("evals") or []
    check("E01 evals.json 可解析且非空", bool(evals), "空的 evals")
    kinds = [e.get("kind") for e in evals]
    retrieval = [e for e in evals if str(e.get("kind", "")).startswith("retrieval")]
    check("E02 含检索类评测（≥5 项）", len(retrieval) >= 5, f"只有 {len(retrieval)} 项")
    check("E03 保留分析类评测（4 项）", kinds.count("analysis") == 4, f"{kinds.count('analysis')} 项")
    hard = {e.get("hardness") for e in retrieval}
    need = {"JS 渲染站", "扫描版 PDF", "Cloudflare / 反爬拦截", "死链与失效链接"}
    check("E04 覆盖关键难抓形态", need.issubset(hard), f"缺 {need - hard}")
    ids = [e.get("id") for e in evals]
    check("E05 id 唯一", len(ids) == len(set(ids)), str(ids))
    check("E06 含幻觉诱导回归题",
          any(e.get("kind") == "retrieval-regression" for e in evals), "缺 regression 题")
    missing = [e.get("id") for e in evals if len(e.get("expectations") or []) < 5]
    check("E07 每题 expectations ≥5 条", not missing, f"id={missing}")


# ============================================================ 2. 本地站点
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: D102
        pass


def start_server(directory: str) -> tuple[socketserver.TCPServer, str]:
    handler = functools.partial(_Quiet, directory=directory)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{port}"


# ============================================================ 3. 采集 + 指标
def run_pipeline(base: str) -> tuple[dict, dict, dict]:
    if os.path.exists(ARCHIVE):
        shutil.rmtree(ARCHIVE, ignore_errors=True)
    os.makedirs(ARCHIVE, exist_ok=True)

    with open(CONFIG, encoding="utf-8") as f:
        template = f.read()
    rendered = template.replace("{BASE}", base)
    rendered_path = CONFIG + ".rendered"
    with open(rendered_path, "w", encoding="utf-8") as f:
        f.write(rendered)

    prepare_proxy_env()
    options = CollectOptions(
        max_fetch=12,
        net=NetConfig(timeout=15, retries=0, sleep_between=0.0, use_proxy=False),
        archive_root=ARCHIVE,
    )
    report, summary = run_collect(rendered_path, ARCHIVE, options)
    index = load_index(ARCHIVE)
    return report.to_dict(), summary, index


def check_metrics(report: dict, index: dict) -> None:
    rows = list((index.get("entries") or {}).values())
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["fetch_status"]] = statuses.get(r["fetch_status"], 0) + 1

    print("\n--- 采集结果分布 ---")
    print("per_source:", json.dumps(report.get("per_source"), ensure_ascii=False))
    print("fetch_status:", json.dumps(statuses, ensure_ascii=False))
    print("full_text_rate:", report.get("full_text_rate"))

    check("M01 采到条目", len(rows) >= 4, f"{len(rows)} 条")
    check("M02 有 full 级证据", statuses.get("full", 0) >= 2, str(statuses))
    check("M03 识别出 listing_only", statuses.get("listing_only", 0) >= 1, str(statuses))
    check("M04 识别出 blocked", statuses.get("blocked", 0) >= 1, str(statuses))

    # 关键质量断言：任何被标 full 的条目，正文里都不得出现 PDF 二进制/镜像工具栏
    bad_full = []
    for r in rows:
        if r["fetch_status"] != "full":
            continue
        head = r.get("head_fingerprint") or ""
        if "%PDF-" in head or "Wayback Machine" in head:
            bad_full.append(r["url"])
    check("M05 full 条目无 PDF/镜像污染", not bad_full, str(bad_full[:3]))

    # 正文获取率必须真实计算，且与 per_source 自洽（报告里做了 4 位小数舍入）
    rate = report.get("full_text_rate")
    expected = (statuses.get("full", 0) / len(rows)) if rows else 0.0
    check("M06 正文获取率计算正确", abs((rate or 0) - expected) < 1e-3,
          f"report={rate} expected={expected}")


# ============================================================ 4. 幻觉回归
def check_hallucination_regression(index: dict) -> None:
    rows = [r for r in (index.get("entries") or {}).values() if r["fetch_status"] == "full"]
    check("H00 存在可用于回归的 full 证据", bool(rows), "无 full 证据")
    if not rows:
        return
    row = rows[0]
    url, docno = row["url"], row["docno"] or "2026-00001"

    good = (
        f"# 报告\n\n- 依据文号：{docno}\n"
        f"- 来源与访问：{url} （访问日期：2026-10-07）\n\n"
        f"> {row['head_fingerprint'][:80]}\n"
    )
    bad = (
        "# 报告\n\n"
        "- 依据文号：XX发〔2099〕99号\n"
        "- 补贴上限：单个机构不超过 8800 万元\n"
        "- 来源：https://example.invalid/fabricated\n\n"
        "> 本法自2099年5月1日起施行，适用于所有跨国机构。\n"
    )
    good_result = vf.verify_report(good, ARCHIVE)
    bad_result = vf.verify_report(bad, ARCHIVE)

    check("H01 合格报告通过 C1–C4", good_result.ok,
          json.dumps([f.to_dict() for f in good_result.errors], ensure_ascii=False)[:300])
    check("H02 编造链接被拦截",
          any(f.check.startswith("C1") for f in bad_result.errors), "C1 未触发")
    check("H03 改写引文被拦截",
          any(f.check.startswith("C2") for f in bad_result.errors), "C2 未触发")
    check("H04 编造文号/金额被拦截",
          any(f.check.startswith("C3") for f in bad_result.errors), "C3 未触发")
    check("H05 编造报告整体不通过", not bad_result.ok, "竟通过了")
    check("H06 统计含证据强度分布", "evidence_counts" in good_result.stats,
          str(good_result.stats))

    print("\n--- 幻觉回归 ---")
    print("合格报告 ok =", good_result.ok, "| stats =", json.dumps(good_result.stats, ensure_ascii=False))
    print("编造报告 ok =", bad_result.ok, "| 拦截项 =",
          [f.check for f in bad_result.errors])


# ============================================================ 5. 强度闸门
def check_strength_gate() -> None:
    check("G01 listing_only 不支撑结论",
          not FetchStatus.LISTING_ONLY.allows_any_conclusion)
    check("G02 blocked 不支撑结论", not FetchStatus.BLOCKED.allows_any_conclusion)
    check("G03 scanned_pdf 不支撑结论", not FetchStatus.SCANNED_PDF.allows_any_conclusion)
    check("G04 partial 不支撑核心结论", not FetchStatus.PARTIAL.allows_core_conclusion)
    check("G05 full 可支撑核心结论", FetchStatus.FULL.allows_core_conclusion)


def main() -> int:
    print("=" * 76)
    print("检索策略评测（离线 fixtures + 本地静态服务器）")
    print("=" * 76)

    check_eval_spec()
    check_strength_gate()

    httpd, base = start_server(SITE)
    try:
        report, summary, index = run_pipeline(base)
        check_metrics(report, index)
        check_hallucination_regression(index)
        if summary.get("failures"):
            print("\n信源失败记录：", json.dumps(summary["failures"], ensure_ascii=False)[:400])
        if summary.get("duplicates"):
            print("去重记录：", json.dumps(summary["duplicates"], ensure_ascii=False)[:300])
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n" + "=" * 76)
    for name in PASSED:
        print("  PASS ", name)
    for name in FAILED:
        print("  FAIL ", name)
    print("=" * 76)
    print(f"通过 {len(PASSED)} 项 / 失败 {len(FAILED)} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
