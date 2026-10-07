# 政策情报检索层 · collector

这一层解决一个具体问题：**AI Agent 经常只能看见标题，看不见正文——却照样写出结论。**

它把「检索」从临场搜索变成可执行的采集工程：声明式信源表 → 分级抓取 → 完整性判定
→ 证据存证 → 确定性幻觉校验。

## 快速开始

本层**零外部依赖安装**：只用 Python 标准库 + 环境中已有的 `httpx` / `lxml` / `pyyaml`。
（装了 `pypdf` 或 `pdfminer.six` 会更好，没装也能解析文字版 PDF。）

```bash
cd global-policy-intelligence/tools

# 0) 环境自检：告诉你哪些能力可用、哪些会降级
python -m collector doctor

# 1) 采集（默认抓 40 篇正文；--sources 留空 = 用 sources.yaml 里 enabled 的信源）
python -m collector collect --sources federal_register,uk_legislation --max-fetch 10

# 2) 只采列表不抓正文（先看有哪些新条目）
python -m collector collect --listing-only --json

# 3) 按时间窗口采集
python -m collector collect --since 2026-09-01 --until 2026-09-30

# 4) 对写好的报告做幻觉校验
python -m collector verify 报告.md --evidence ../evidence
```

退出码约定：`0` 成功；`2` 校验未通过或缺依赖；`3` 有信源失败（让定时任务能看见，
而不是假装一切正常）。

## 产物

| 路径 | 内容 |
|---|---|
| `../evidence/index.json` | 证据台账（19 字段，含 `fetch_status` / 快照哈希 / 首尾指纹 / attempts） |
| `../evidence/snapshots/<年>/<月>/<源>/<id>.json` | 正文快照（可审计、可复查、可重建引用） |
| `../evidence/<日期>_全文存档.md` | 人类可读全文存档（未取得正文的条目会写明原因与已试路径） |
| `tools/evals/.evidence/` | 离线评测用的证据库（可随时删除重建） |

## 架构

```
collector/
  net.py        HTTP 客户端：代理分流修复、限速、重试、内容类型判定、尝试记录
  registry.py   sources.yaml 声明式信源注册表（含 probe 实测状态）
  listparse.py  列表解析：JSON / HTML / RSS / Atom / sitemap
  extract.py    正文与元数据抽取 + 完整性四态判定（防"假正文"）
  pdfx.py       PDF 正文提取（零依赖优先；扫描件显式失败）
  fetch.py      正文回落链：api → text → html → pdf → render → mirror → 明确失败
  store.py      快照 / 证据索引 / 内容级去重 / 全文存档
  verify.py     确定性幻觉校验 C1–C4
  run.py        采集编排
  cli.py        命令行入口
```

## 三个真实踩过的坑（都已修复并有回归测试）

1. **PDF 二进制被当成正文**：详情页链接直接返回 PDF 时，若按 URL 后缀判断就会把二进制
   存进证据库并标成 `full`。现在一律靠内容判定（`%PDF-` magic bytes）。
   → 回归测试 T24/T25
2. **`str in bytes` 导致静态 HTML 源全线静默失败**：`"%PDF-" in raw[:1024]`（str 找 bytes）
   抛 `TypeError`，又被上层 `try` 吞掉，结果是**每一个静态 HTML 源都变成 blocked**，
   报告看起来像"这些来源没有动态"。这是本项目最危险的一类 bug。
   → 回归测试 T27（用本地服务器 + 真实 Fetcher 端到端跑）
3. **镜像兜底把检索工具自己的界面当正文**：Wayback 默认快照地址返回工具栏页面（仅 207 字
   全是短行链接）却越过了 200 字阈值。现在必须用 `id_` 原始内容地址，并识别导航/工具页。
   → 回归测试 T26

## 环境相关的两个已知问题

- **代理**：本机环境的 `NO_PROXY` 含 `[::1]` 这种写法，会让 httpx 抛
  `InvalidURL: Invalid port`，导致**所有**请求失败。`net.py` 会在导入时修好它。
  另外实测本机代理在部分境内政府站上 TLS 握手失败而直连正常，因此实现了
  **按域名分流 + 代理失败自动改直连**。
- **写入权限**：Windows 沙箱下 `tempfile.mkdtemp` 创建的目录无法写入，
  因此测试与证据库都建在工作区内的普通目录。

## 能力边界（必须如实披露）

- 未配置 `POLICY_INTEL_RENDER_CMD` 时，JS 渲染站一律标 `blocked`；
- 未装 `pypdf`/`pdfminer` 时，扫描版 PDF 会被识别为 `scanned_pdf` 而非正文；
- 不绕过验证码、登录墙、付费墙——遇对抗性防护即标 blocked 并给替代路径；
- `verify` 只能保证"可核验事实都在证据里"，**不能**判断结论方向是否正确。

配置渲染器（可选）：

```bash
# 任何"接受 URL、把渲染后 HTML 打到 stdout"的命令都可以
set POLICY_INTEL_RENDER_CMD=my-render-cli
```

## 测试与评测

```bash
python tests/run_tests.py              # 72 项离线回归（含上述三大坑）
python evals/make_pdf_fixture.py       # 生成评测用 PDF fixture
python evals/run_retrieval_evals.py    # 25 项检索策略评测（本地静态服务器，不联网）
```
