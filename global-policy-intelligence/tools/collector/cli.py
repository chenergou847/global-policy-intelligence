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

# 依赖清单（单一来源：改包名/加依赖只需动这里）
#   导入名 → (PyPI 发行名, 是否必需, 用途)
DEPS: dict[str, tuple[str, bool, str]] = {
    "httpx": ("httpx", True, "HTTP 客户端：分级抓取"),
    "lxml": ("lxml", True, "HTML/XML 解析：正文抽取"),
    "yaml": ("PyYAML", True, "读取 sources.yaml 信源注册表"),
    "pypdf": ("pypdf", False, "可选：更完整的 PDF 正文提取"),
    "pdfminer": ("pdfminer.six", False, "可选：PDF 正文提取备选"),
    "cssselect": ("cssselect", False, "可选：CSS 选择器加速"),
}
# 实际参与自检的包（pdfminer 与 pypdf 二者其一即可，故只列主要项）
CHECK_MODULES = ("httpx", "lxml", "yaml", "pypdf", "pdfminer", "cssselect")


def _try_import(name: str) -> bool:
    """真正 import 一次，而不是只用 importlib.util.find_spec。

    find_spec 只负责「定位」模块文件、不执行它。若某模块文件存在但导入时抛异常
    （二进制不兼容、缺子依赖、被影子模块顶替等），find_spec 会报告"在"，
    而真实运行会崩——那就失去了提前检查的意义（实测踩到过）。
    """
    import importlib

    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _capabilities() -> dict[str, object]:
    caps: dict[str, object] = {"python": sys.version.split()[0]}
    for name in CHECK_MODULES:
        caps[name] = _try_import(name)
    caps["render_cmd"] = os.environ.get("POLICY_INTEL_RENDER_CMD", "")
    # 代理自检：本环境 NO_PROXY 若含 [::1] 会让 httpx 全部请求失败，已在 net.py 修复
    caps["no_proxy_sanitized"] = True
    return caps


def _missing_required() -> list[str]:
    """缺少的必需依赖（顺序稳定，便于断言与提示）。"""
    return [m for m in DEPS if DEPS[m][1] and not _try_import(m)]


def _install_hint(names: list[str]) -> str:
    """导入名与 PyPI 发行名不一定相同（yaml → PyYAML），否则给出的安装命令会失败。"""
    return "python -m pip install " + " ".join(DEPS[n][0] if n in DEPS else n for n in names)


def _require_deps() -> int | None:
    """缺依赖时给出可操作的提示，而不是抛裸 traceback。返回退出码或 None（可用）。"""
    missing = _missing_required()
    if not missing:
        return None
    print("缺少必需的第三方依赖：" + "、".join(missing), file=sys.stderr)
    print("安装：" + _install_hint(missing), file=sys.stderr)
    print("自检：python -m collector doctor", file=sys.stderr)
    print("（说明：本工具不需要 clone 之外的额外下载，但这几个包必须在运行环境里可用。）",
          file=sys.stderr)
    return 2


def cmd_doctor(_args: argparse.Namespace) -> int:
    caps = _capabilities()
    print("== 运行环境自检 ==")
    for key, value in caps.items():
        print(f"  {key}: {value}")
    missing = [m for m in CHECK_MODULES if not caps.get(m)]
    required_missing = [m for m in missing if DEPS.get(m, ("", False, ""))[1]]
    if required_missing:
        print(f"\n[错误] 缺少必需依赖: {', '.join(required_missing)}")
        print("安装：" + _install_hint(required_missing))
        return 2
    if not caps.get("pypdf") and not caps.get("pdfminer"):
        print("\n[提示] 未安装 pypdf/pdfminer，PDF 将使用内置标准库解析器（覆盖文字版 PDF，"
              "扫描件会被标记为 scanned_pdf）。")
    if not caps.get("render_cmd"):
        print("[提示] 未配置 POLICY_INTEL_RENDER_CMD，JS 渲染站将标记为 blocked。")
    print("\n结论：基础能力可用。")
    return 0


def _source_keys(args: argparse.Namespace) -> list[str]:
    return [s.strip() for s in (args.sources or "").split(",") if s.strip()]


def _print_dry_run(summary: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print("== DRY RUN：未发送任何请求 ==")
    print(f"将访问 {summary['sources_selected']} 个信源，"
          f"列表请求 {summary['listing_requests_would_send']} 次，"
          f"正文抓取预算 {summary['detail_fetch_budget']} 篇")
    print()
    for item in summary["plan"]:
        print(f"  [{item['key']}] {item['name']}  (probe={item['probe']}, type={item['type']})")
        for url in item["listing_requests"]:
            print(f"      列表 → {url}")
        print(f"      正文 → mode={item['detail_mode']} tiers={item['detail_tiers']}"
              f" 最多 {item['declared_max_items']} 条")
    print()
    print(summary["note"])


def _warn_placeholder_contact(config_path: str) -> None:
    """默认配置里的联系邮箱是占位符时提醒一次（礼貌采集）。"""
    try:
        with open(config_path, encoding="utf-8") as f:
            head = f.read(4000)
    except OSError:
        return
    if "请在此填写你的邮箱" in head:
        print("[提醒] sources.yaml 的 meta.contact_ua 仍是占位邮箱。"
              "正式采集前建议改成你的真实联系方式，便于站点管理员联系；"
              "或用 --dry-run 先查看将要请求的地址。", file=sys.stderr)


def _run_dry_run(args: argparse.Namespace) -> int:
    """--dry-run：只读配置、不发请求。只需 pyyaml，不需要 httpx/lxml。"""
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
        sources=_source_keys(args),
        max_fetch=args.max_fetch,
        listing_only=args.listing_only,
    )
    _print_dry_run(summary, args.json)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    # dry-run 只需要 PyYAML（读配置），不需要 httpx/lxml：
    # 缺依赖的用户最该先跑这个命令，所以不能被联网/解析能力拦住
    # （这是实测发现的设计缺陷：原先它和真实采集共用同一条依赖检查）。
    if args.dry_run:
        return _run_dry_run(args)

    blocked = _require_deps()
    if blocked is not None:
        return blocked
    from .net import NetConfig
    from .run import CollectOptions, run_collect

    _warn_placeholder_contact(args.config)

    opts = CollectOptions(
        sources=_source_keys(args),
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
