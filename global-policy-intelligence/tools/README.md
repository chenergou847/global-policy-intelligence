# 采集与逐项证据校验

从本目录运行 `python -m collector`。先按任务选择法域和信源；默认注册表仅包含美国和英国官方适配器，不代表全球覆盖。

```bash
python -m collector doctor
python -m collector collect --sources nfra_dongtai --dry-run
python -m collector collect --sources federal_register --since 2026-09-01 --until 2026-09-30 --max-fetch 10 --evidence /path/to/project/evidence
python -m collector verify report.md --claims claims.json --evidence /path/to/project/evidence
```

`--sources` 可显式选择默认停用的源；未知 key 会报错。正文预算必须大于零，纯列表任务使用 `--listing-only`。计划与真实采集共用信源选择和分页逻辑。

## 网络

HTTPX 读取 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY；本机请求默认绕过代理。`--direct` 忽略全部环境代理；`--no-direct-fallback` 禁用网络错误后的直连回落。没有全局官方域名直连假设。HTTP 403/404/410 不通过切换路径绕过。

响应以流方式读取并限制解码后的大小。超限内容被拒绝，不会伪装成完整正文。每篇条目的 attempts 只记录该篇的抓取请求。有效的 ASCII `meta.contact_ua` 会用于 HTTP 请求头。

## 存证

`index.json` 为索引；`snapshots/` 为正文与抓取状态快照。索引原子写入，损坏索引明确报错。同 URL 的变化保留历史快照引用。快照读取只允许证据库内路径。

去重保留更完整、更权威的副本。同标题同月份不足以证明同一政策；不同文号或修订内容必须保留。

## 校验

`verify` 默认严格模式，缺少 claims ledger 返回 2。每项核心事实/推论需引用可读 full L1/L2 快照；引文和事实数字只在该项引用的快照中核对。

`company` 和 `assumption` 声明不作为公开政策事实自动核验，须在报告中明确标记。`inference` 的成立条件、因果和适用范围仍需人工检查。脚本不能自动证明台账覆盖了所有重要正文。

`--legacy-checks-only` 可运行原有 C1-C4 全局匹配，仅用于迁移。不能据此声称报告通过严格验收。

退出码：0 为已执行检查通过/计划成功/采集成功；2 为输入、依赖或审计失败；3 为采集受限（信源失败、无正文等）。0 不表示检索覆盖完整或分析正确。

## 开发

```bash
python check.py
python -m unittest discover -s tests -p "test_*.py"
```

fixtures 与本机 HTTP 服务用于稳定回归；外部政府站点可用性需单独核验。分析评估见 `../evals/README.md`。
