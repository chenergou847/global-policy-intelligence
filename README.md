# 全球政策与监管情报分析

面向**企业管理层、公共事务、法务和财务团队**的全球政策与监管情报分析技能。

用于分析国家、地区、省州、市县区层面的政策方向、法律法规、监管与执法趋势、地方实施
和行业舆情，解释政策背后的**宏观与治理动因**及其对**具体公司**的影响，输出总体利好/利空
判断、P0–P3 合规风险与政策机会、行动责任和未来关注点。

---

## 怎么用（两步）

### 第一步：装到你的 Agent 里，三选一

#### ① 让 Agent 自己装（最省事）—— 直接对它说：

```
帮我安装这个 Skill：https://github.com/chenergou847/global-policy-intelligence
装到你的 Skills 目录，并装好依赖：pip install -r global-policy-intelligence/requirements.txt
（必需依赖只有三个：httpx、lxml、PyYAML；装完用 python -m collector doctor 自检）
```

Agent 会自己 clone、放进 Skills 目录、装依赖并跑自检。装完把 `doctor` 的输出贴给你看即可。

#### ② 下载 ZIP

点仓库右上角 **Code → Download ZIP**，解压得到 `global-policy-intelligence` 文件夹，
放进你 Agent 的 Skills 目录（各 Agent 不同，见下表）。

#### ③ git clone

```bash
git clone https://github.com/chenergou847/global-policy-intelligence
pip install -r global-policy-intelligence/requirements.txt    # 装依赖（在刚 clone 出的目录里执行）
# 再把 global-policy-intelligence 文件夹放进 Skills 目录，
# 例如 ~/.codex/skills/global-policy-intelligence（各 Agent 的目录不同）
```

> `requirements.txt` 在 **Skill 根目录**（`global-policy-intelligence/`）下。
> 所以无论你是在仓库根目录还是已经进了 Skill 目录，`pip install -r requirements.txt`
> 都能找到它——只要当前目录对得上。

**常见 Agent 的 Skills 目录**（不确定就先问你的 Agent "你的 skills 目录在哪"）：

| Agent | 目录 |
|---|---|
| Codex CLI | `~/.codex/skills/` |
| Claude Code | `~/.claude/skills/` |
| DSH | `~/.dsh/skills/` |
| 其他 | 多数是 `~/.<agent>/skills/`；也支持在项目内放 `.skills/` 或 `.agent/skills/` |

> 装的是**整个仓库**（含 `tools/` 采集层），不是只复制 `SKILL.md`——
> 采集层是这个技能能拿到正文、不瞎写结论的关键。

### 第二步：装完先自检，再开始用

```bash
cd <你的 Skills 目录>/global-policy-intelligence/tools

python -m collector doctor          # ① 自检：哪些能力可用、哪些会降级
python -m collector collect --dry-run   # ② 看清将要请求哪些地址（不发任何请求）
python -m collector collect --max-fetch 10   # ③ 小批量真跑一次，确认能拿到正文
```

`doctor` 会明确告诉你缺什么、什么会降级。第 ③ 步跑通后，就可以直接把任务交给 Agent
（"帮我分析最近 3 个月欧盟数据跨境规则对我们新加坡托管业务的影响"），
它会在需要时调用采集层并用 `verify` 做幻觉校验。

> **首次采集前建议**：把 `tools/sources.yaml` 里 `meta.contact_ua` 的占位邮箱
> 换成你的真实联系方式——采集会在 User-Agent 里带上它，便于站点管理员识别与联系。

### 三条注意

1. **必需依赖只有三个**（`httpx` / `lxml` / `PyYAML`），已在 `requirements.txt` 里。
   缺任何一个都会**明确报错并给出安装命令**，不会静默降级、也不会跳过校验硬跑。
2. **`--dry-run` 只需要 `PyYAML`**：它只读 `sources.yaml`、不发请求、不解析 HTML，
   所以缺 `httpx`/`lxml` 时照常可用，专门给"先看看它会请求谁"这个场景。
   但缺 `PyYAML` 它也跑不了（读不了配置）——三者里它是最靠底层的一个。
3. **采集层不会自动装依赖**：它只把安装命令打给你，装不装、装到哪个 Python 环境由你决定。

---

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
├── requirements.txt                  # 采集层依赖（httpx / lxml / PyYAML）
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
└── tools/                            # ★ 可执行采集层（依赖见上方的 requirements.txt）
    ├── sources.yaml                  # 声明式信源注册表（含 probe 实测状态）
    ├── collector/                    # 分级采集 / 正文回落链 / 存证 / 幻觉校验
    ├── tests/                        # 85 项离线回归
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

## 采集层命令参考

装好之后（见开头「怎么用」），`cd <Skills 目录>/global-policy-intelligence/tools` 后可用：

```bash
python -m collector doctor                    # 环境与能力自检
python -m collector collect --dry-run         # 只打印将要请求的地址，不发任何请求
python -m collector collect --max-fetch 10    # 采集正文（建议先设小预算）
python -m collector collect --listing-only --json   # 只采列表，看有哪些新条目
python -m collector collect --since 2026-09-01 --until 2026-09-30   # 按时间窗口
python -m collector verify 报告.md --evidence ../evidence           # 校验报告有无幻觉
```

**前提**：Python 3.10+，且运行环境里已有 `requirements.txt` 中的三个包
（`httpx` / `lxml` / `PyYAML`）。这里的"零外部依赖"指的是**不需要额外下载本项目所需的
任何东西**（没有自研 pip 包、没有二进制、没有构建步骤），**不是指任何 Python 环境都能直接跑**。

装了 `pypdf` 或 `pdfminer.six` 会更好（PDF 正文提取更完整），没装也能解析文字版 PDF，
扫描件会被明确标记为 `scanned_pdf` 而不是当成正文。

### 缺依赖时到底会怎样（实测）

**不会自动安装**，也不会降级成"跳过校验照样跑"——命令直接停，并告诉你怎么修。
下面的行为是逐项实测的结果：

| 命令 | 缺 `httpx` 或 `lxml` | 缺 `PyYAML` | 退出码 |
|---|---|---|---|
| `doctor` | 正常跑完并列出缺哪个 | 正常跑完并列出缺哪个 | 0 齐全 / 2 有缺 |
| `collect --dry-run` | **照常出计划**（只读配置，不需要联网/解析） | 报错并给安装命令 | 0 / 2 |
| `collect`（真实采集） | 报错并给安装命令 | 报错并给安装命令 | 2 |
| `verify` | 报错并给安装命令 | 报错并给安装命令 | 2 |

报错时给出的是可直接执行的修复命令，不是裸 `traceback`：

```
$ python -m collector collect
缺少必需的第三方依赖：httpx、lxml、yaml
安装：python -m pip install httpx lxml PyYAML
自检：python -m collector doctor
```

设计取舍：`doctor` 与 `collect --dry-run` 刻意不依赖 `httpx`/`lxml`，
因为它们只是"读配置 + 打印"，**这正是缺依赖的用户最该先跑的两条命令**；
真实采集与幻觉校验则需要联网与 HTML 解析能力——**拿不到正文就不该出结论**，
所以它们宁可停下也不降级。

超出本工具能力的是：**它不会替用户执行 `pip install`**。Agent 会看到上面那段
提示，但要不要装、用哪个 Python 环境装，需要由你决定。

## 测试与评测

```bash
cd global-policy-intelligence/tools
python tests/run_tests.py              # 85 项离线回归
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
