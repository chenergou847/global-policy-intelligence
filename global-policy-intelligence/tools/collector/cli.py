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
    import importlib

    caps: dict[str, object] = {}
    caps["python"] = sys.version.split()[0]
    # 注意用 import_module 而不是 find_spec：后者只定位文件、不执行导入，
    # 遇到"文件在但导入失败"（二进制不兼容等）会误报为可用。
    for name in ("httpx", "lxml", "yaml", "pypdf", "pdfminer", "cssselect", "playwright"):
        try:
            importlib.import_module(name)
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


def _missing_required() -> list[str]:
    """报告缺少的必需第三方依赖。

    必须真正 import，而不是只用 importlib.util.find_spec：
    find_spec 只负责「定位」模块文件，不执行它。若某模块文件存在但导入时
    抛 ImportError（二进制不兼容、依赖缺失、影子文件等），find_spec 会说"在"，
    而真实运行会崩——这就失去了提前检查的意义（实测踩到）。
    """
    import importlib

    missing: list[str] = []
    for mod in ("httpx", "lxml", "yaml"):
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001  ImportError / OSError / 二进制不兼容都算不可用
            missing.append(mod)
    return missing


def _require_deps() -> int | None:
    """缺依赖时给出可操作的提示，而不是抛裸 traceback。返回退出码或 None（可用）。"""
    missing = _missing_required()
    if not missing:
        return None
    # 导入名与 PyPI 发行名不一定相同（yaml → PyYAML），否则给出的安装命令会失败
    dist = {"yaml": "PyYAML", "lxml": "lxml", "httpx": "httpx"}
    print("缺少必需的第三方依赖：" + "、".join(missing), file=sys.stderr)
    print("安装：python -m pip install " + " ".join(dist.get(m, m) for m in missing), file=sys.stderr)
    print("自检：python -m collector doctor", file=sys.stderr)
    print("（说明：本工具不需要 clone 之外的额外下载，但这几个包必须在运行环境里可用。）",
          file=sys.stderr)
    return 2


def cmd_collect(args: argparse.Namespace) -> int:
    # dry-run 只需要 PyYAML（读配置），不需要 httpx/lxml：
    # 缺依赖的用户最该先跑这个命令，所以不能被联网/解析能力拦住
    # （这是实测发现的设计缺陷：原先它和真实采集共用同一条依赖检查）。
    if args.dry_run:
        # 但它确实需要 pyyaml 才能读 sources.yaml——缺了要给提示而不是裸 traceback
        if "yaml" in _missing_required():
            return _require_deps() or 2
        if not os.path.isfile(args.config):
            print(f"找不到信源配置：{args.config}", file=sys.stderr)
            print("提示：--config 默认指向 tools/sources.yaml，请确认当前目录或显式指定。",
                  file=sys.stderr)
            return 2
        from .dryrun import build_dry_run_plan

        summary = build_dry_run_plan(
            args.config,
            sources=[s.strip() for s in (args.sources or "").split(",") if s.strip()],
            max_fetch=args.max_fetch,
            listing_only=args.listing_only,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print("== DRY RUN：未发送任何请求 ==")
            print(f"将访问 {summary['sources_selected']} 个信源，"
                  f"列表请求 {summary['listing_requests_would_send']} 次，"
                  f"正文抓取预算 {summary['detail_fetch_budget']} 篇")
            print()
            for p in summary["plan"]:
                print(f"  [{p['key']}] {p['name']}  (probe={p['probe']}, type={p['type']})")
                for u in p["listing_requests"]:
                    print(f"      列表 → {u}")
                print(f"      正文 → mode={p['detail_mode']} tiers={p['detail_tiers']}"
                      f" 最多 {p['declared_max_items']} 条")
            print()
            print(summary["note"])
        return 0

    blocked = _require_deps()
    if blocked is not None:
        return blocked
    from .net import NetConfig
    from .run import CollectOptions, run_collect

    # 礼貌提醒：默认配置里的联系邮箱是占位符，采集前应换成真实联系方式
    if not args.dry_run and os.path.exists(args.config):
        try:
            with open(args.config, encoding="utf-8") as f:
                head = f.read(4000)
            if "请在此填写你的邮箱" in head:
                print("[提醒] sources.yaml 的 meta.contact_ua 仍是占位邮箱。"
                      "正式采集前建议改成你的真实联系方式，便于站点管理员联系；"
                      "或用 --dry-run 先查看将要请求的地址。", file=sys.stderr)
        except OSError:
            pass

    opts = CollectOptions(
        sources=[s.strip() for s in (args.sources or "").split(",") if s.strip()],
        since=args.since,
        until=args.until,
        max_fetch=args.max_fetch,
        listing_only=args.listing_only,
        dry_run=args.dry_run,
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
    # 先做输入检查，给出可操作的提示而不是让 open() 抛 FileNotFoundError
    if not os.path.isfile(args.report):
        print(f"找不到报告文件：{args.report}", file=sys.stderr)
        print("用法：python -m collector verify <报告.md> --evidence <证据库目录>", file=sys.stderr)
        return 2
    if not os.path.isdir(args.evidence):
        print(f"找不到证据库目录：{args.evidence}", file=sys.stderr)
        print("提示：先运行 python -m collector collect 生成证据库。", file=sys.stderr)
        return 2

    blocked = _require_deps()
    if blocked is not None:
        return blocked
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
    c.add_argument("--dry-run", action="store_true",
                   help="只打印将要请求的地址与抓取预算，不发送任何请求（建议首次先跑这个）")
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
