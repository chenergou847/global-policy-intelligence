# 全球政策与监管情报分析

面向**企业管理层、公共事务、法务和财务团队**的全球政策与监管情报分析技能。

用于分析国家、地区、省州、市县区层面的政策方向、法律法规、监管与执法趋势、地方实施
和行业舆情，解释政策背后的**宏观与治理动因**及其对**具体公司**的影响，并解释政策背后的
宏观与治理动因，输出总体利好/利空判断、P0–P3 合规风险与政策机会、行动责任和未来关注点。

## 输出内容

| 输出项 | 说明 |
| --- | --- |
| 总体判断 | 利好 / 利空 / 中性偏利好 / 中性偏利空，附影响程度与置信度 |
| 合规风险 | 按 P0–P3 分级 |
| 政策机会 | 可争取的政策窗口，与风险分开列账 |
| 行动责任 | 动作、责任角色、时限、依赖、验收标准 |
| 未来关注点 | 需持续跟踪的信号与下一复查日 |
| 来源台账 | 19 字段可审计证据（含正文状态、快照哈希、逐字引文） |

## 内置专题分析框架

针对国际政治经济议题设有专门框架：地缘政治、国别政治生态、科技竞争与供应链重构、
制裁与出口管制。

## 目录结构

```
global-policy-intelligence/
├── SKILL.md                          # 七步工作流程（含第三步检索与核验纪律）
├── references/
│   ├── retrieval-pipeline.md         # ★ 七层检索流水线（本技能的核心方法论）
│   ├── evidence-and-sources.md       # 证据分级、禁止来源、台账 19 字段
│   ├── intake-and-scope.md           # 范围与时间尺度校准
│   ├── policy-analysis-framework.md  # 政策动因与传导链
│   ├── impact-and-priority.md        # 影响评估与 P0–P3
│   ├── geopolitics-and-tech-competition.md
│   ├── report-template.md            # 报告模板（含 2.5 检索与核验说明）
│   └── quality-and-boundaries.md      # 交付前核查（含五个阻断项）
├── evals/evals.json                  # 11 项评测：4 项分析质量 + 7 项检索策略
└── tools/                            # ★ 可执行采集层（零外部依赖安装）
    ├── sources.yaml                  # 声明式信源注册表（含 probe 实测状态）
    ├── collector/                    # 分级采集 / 正文回落链 / 存证 / 幻觉校验
    ├── tests/                        # 72 项离线回归
    └── evals/                        # 25 项检索策略评测
```

## 为什么需要采集层

AI Agent 做政策检索时最常见的失败不是"搜不到"，而是：

> **只看见标题、摘要或 JS 骨架页，却照样写出结论。**

本技能用两层设计把这个漏洞堵住：

**1. 结论强度由材料级别决定（硬闸门）**

| `fetch_status` | 含义 | 允许的结论强度 |
|---|---|---|
| `full` | 取得完整正文 | 可支撑核心结论（仍须附逐字引文） |
| `partial` | 正文被截断或缺字段 | 只能中/低置信度 |
| `listing_only` | 只有标题与链接 | **不得支撑任何事实性结论** |
| `scanned_pdf` | PDF 无文字层 | 须 OCR/VLM，否则等同 `listing_only` |
| `blocked` | 反爬/登录墙/渲染失败 | **只能写"未取得正文"** |
| 模型自身知识 | 未检索 | **不得出现在任何政策事实、文号、日期中** |

**2. 可执行校验（C1–C4）**

```bash
cd tools
python -m collector verify 报告.md --evidence ../evidence
```

| 检查 | 拦截的幻觉类型 |
|---|---|
| C1 引用链接 | 编造链接、"看起来很像真的"但从未访问的 URL |
| C2 逐字引文 | 改写式引用、凭记忆编造条款 |
| C3 文号数字 | 编造文号、金额、门槛、日期 |
| C4 证据强度 | "只有标题却写出结论" |

> 边界：C1–C4 只保证"可核验事实都在证据里"，**不判断结论方向是否正确**。

## 快速开始

**前提**：Python 3.10+，且运行环境里已有 `httpx`、`lxml`、`PyYAML` 三个包。

> **"零外部依赖安装"是指不需要额外下载本项目所需的任何东西**（没有自研 pip 包、
> 没有二进制、没有构建步骤），**不是指任何 Python 环境都能直接跑**。
> 缺了上面三个包会直接报错——先用 `doctor` 自检，它会告诉你缺什么。

```bash
# 在 clone 出来的仓库根目录执行（即包含 global-policy-intelligence/ 的那一层）
cd global-policy-intelligence/tools

python -m collector doctor          # 环境与能力自检：哪些能力可用、哪些会降级
python -m collector collect --dry-run   # 先看清将要请求哪些地址，不发任何请求
python -m collector collect --max-fetch 10   # 确认后再真正采集（建议先设小预算）
python -m collector verify 报告.md --evidence ../evidence
```

**首次使用建议顺序**：`doctor` → 在 `sources.yaml` 里把 `meta.contact_ua` 的占位邮箱
换成你的真实联系方式 → `collect --dry-run` 核对目标地址 → `collect --max-fetch N` 小批量试跑。

缺少依赖时不会抛裸 `traceback`，而是给出可直接执行的修复命令：

```
$ python -m collector collect
缺少必需的第三方依赖：httpx、lxml、yaml
安装：python -m pip install httpx lxml PyYAML
自检：python -m collector doctor
```

装了 `pypdf` 或 `pdfminer.six` 会更好（PDF 正文提取更完整），没装也能解析文字版 PDF，
扫描件会被明确标记为 `scanned_pdf` 而不是当成正文。

## 测试与评测

```bash
cd global-policy-intelligence/tools
python tests/run_tests.py              # 72 项离线回归
python evals/make_pdf_fixture.py       # 生成评测用 PDF fixture
python evals/run_retrieval_evals.py    # 25 项检索策略评测（本地服务器，不联网）
```

两个主指标：**正文获取率**（`full` 条目占比）与**引用可核验率**
（台账中 `URL + 访问日期 + 快照哈希 + 逐字引文` 四项齐全比例，目标 100%）。

## 合规

只采集公开渠道信息；保留原文链接与快照以便核验；不使用任何内部资料；
**不绕过验证码、登录墙、付费墙**——遇对抗性防护即标记并给出替代路径。

## 专业边界

本技能提供政策与经营影响分析，不替代当地执业律师、税务师或其他专业机构的正式意见。
