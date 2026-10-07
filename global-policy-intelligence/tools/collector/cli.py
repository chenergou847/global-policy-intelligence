# -*- coding: utf-8 -*-
"""collector · 命令行入口

    python -m collector collect --sources nfra_dongtai --max-fetch 8
    python -m collector verify 报告.md --evidence evidence
    python -m collector doctor          # 检查环境与依赖能力

设计原则：**默认安全**。不指定信源时用 sources.yaml 里 enabled: true 的，
不指定窗口时不猜；正文抓取有预算上限，避免误伤站点。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
DEFAULT_SOURCES = os.path.join(TOOLS_DIR, "sources.yaml")
DEFAULT_ARCHIVE = os.path.join(os.path.dirname(TOOLS_DIR), "evidence")


def _capabilities() -> dict[str, object]:
    caps: dict[str, object] = {}
    caps["python"] = sys.version.split()[0]
    for name in ("httpx", "lxml", "yaml", "pypdf", "pdfminer", "cssselect", "playwright"):
        try:
            __import__(name)
            caps[name] = True
        except Exception:  # noqa: BLE001
            caps[name] = False
    caps["render_cmd"] = os.environ.get("POLICY_INTEL_RENDER_CMD", "")
    caps["mirror_enabled"] = not os.environ.get("POLICY_INTEL_NO_MIRROR")
    # 代理自检：本环境 NO_PROXY 若含 [::1] 会让 httpx 全部请求失败，已在 net.py 修复
    caps["no_proxy_sanitized"] = True
    return caps


def cmd_doctor(_args: argparse.Namespace) -> int:
    caps = _capabilities()
    print("== 运行环境自检 ==")
    for k, v in caps.items():
        print(f"  {k}: {v}")
    missing = [k for k in ("httpx", "lxml", "yaml") if not caps.get(k)]
    if missing:
        print(f"\n[错误] 缺少必需依赖: {', '.join(missing)}")
        return 2
    if not caps.get("pypdf") and not caps.get("pdfminer"):
        print("\n[提示] 未安装 pypdf/pdfminer，PDF 将使用内置标准库解析器（覆盖文字版 PDF，"
              "扫描件会被标记为 scanned_pdf）。")
    if not caps.get("render_cmd"):
        print("[提示] 未配置 POLICY_INTEL_RENDER_CMD，JS 渲染站将标记为 blocked。")
    print("\n结论：基础能力可用。" if not missing else "\n结论：不可用。")
    return 0 if not missing else 2


def cmd_collect(args: argparse.Namespace) -> int:
    from .net import NetConfig
    from .run import CollectOptions, run_collect

    opts = CollectOptions(
        sources=[s.strip() for s in (args.sources or "").split(",") if s.strip()],
        since=args.since,
        until=args.until,
        max_fetch=args.max_fetch,
        listing_only=args.listing_only,
        net=NetConfig(
            timeout=args.timeout,
            retries=args.retries,
            sleep_between=args.sleep,
            verify_tls=not args.insecure,
        ),
    )
    report, summary = run_collect(args.config, args.evidence, opts)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"== 采集完成 {report.started_at} → {report.finished_at} ==")
        print(f"条目总数：{len(report.articles)}｜正文获取率：{report.full_text_rate:.1%}")
        for key, row in report.per_source.items():
            print(
                f"  {key}: 列表 {row.get('listed', 0)} → 保留 {row.get('kept', 0)}"
                f"｜full {row.get('full', 0)} / partial {row.get('partial', 0)}"
                f" / listing_only {row.get('listing_only', 0)} / blocked {row.get('blocked', 0)}"
            )
        if summary["duplicates"]:
            print(f"去重：{len(summary['duplicates'])} 条被判定为重复")
        if summary["failures"]:
            print("信源失败（显式记录，不静默丢弃）：")
            for f in summary["failures"]:
                print(f"  - {f['source']} [{f['stage']}]: {f['error']}")
        if summary["archive_path"]:
            print(f"全文存档：{summary['archive_path']}")
        print(f"证据索引：{summary['ledger_path']}")

    # 退出码约定：有信源失败 → 3（让 CI/定时任务能看见，而不是假装成功）
    return 3 if summary["failures"] else 0


def cmd_verify(args: argparse.Namespace) -> int:
    from . import verify as verify_mod

    argv = [args.report, "--evidence", args.evidence]
    if args.json:
        argv.append("--json")
    if args.allow_unknown_citations:
        argv.append("--allow-unknown-citations")
    return verify_mod.main(argv)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="collector",
        description="政策情报检索层：分级采集 + 正文回落链 + 证据存证 + 幻觉校验",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="按信源配置采集并抓正文")
    c.add_argument("--config", default=DEFAULT_SOURCES, help="sources.yaml 路径")
    c.add_argument("--evidence", default=DEFAULT_ARCHIVE, help="证据库根目录")
    c.add_argument("--sources", default="", help="逗号分隔的信源 key；留空=全部 enabled")
    c.add_argument("--since", default=None, help="窗口起点 YYYY-MM-DD")
    c.add_argument("--until", default=None, help="窗口终点 YYYY-MM-DD")
    c.add_argument("--max-fetch", type=int, default=40, help="本次最多抓多少篇正文")
    c.add_argument("--listing-only", action="store_true", help="只采集列表，不抓正文")
    c.add_argument("--timeout", type=float, default=25.0)
    c.add_argument("--retries", type=int, default=2)
    c.add_argument("--sleep", type=float, default=0.8, help="同域请求间隔（秒）")
    c.add_argument("--insecure", action="store_true", help="关闭 TLS 校验（不推荐）")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_collect)

    v = sub.add_parser("verify", help="对报告做确定性幻觉校验")
    v.add_argument("report", help="报告 Markdown 路径")
    v.add_argument("--evidence", default=DEFAULT_ARCHIVE, help="证据库根目录")
    v.add_argument("--json", action="store_true")
    v.add_argument("--allow-unknown-citations", action="store_true")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("doctor", help="环境与能力自检")
    d.set_defaults(func=cmd_doctor)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
