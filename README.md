# 全球政策与监管情报分析

将官方政策变化转化为企业影响、优先级和行动。适用于政策影响分析、监管扫描、多法域比较与既有报告更新。

技能目录是仓库中的 **`global-policy-intelligence/` 子目录**，不是仓库根目录。安装后，`SKILL.md` 必须直接位于技能文件夹内。

## 安装

```bash
git clone https://github.com/chenergou847/global-policy-intelligence.git
cd global-policy-intelligence
python -m pip install -r global-policy-intelligence/requirements.txt
```

将当前仓库下的 `global-policy-intelligence/` 子目录整体复制到 Agent 的技能目录。例如 Codex 的目标为 `~/.codex/skills/global-policy-intelligence/`。保留其中的 `SKILL.md`、`references/`、`tools/`、`evals/` 和 `requirements.txt`。

下载 ZIP 时也要打开外层仓库文件夹，再复制内层技能文件夹。安装工具支持 GitHub 子路径时，使用 `global-policy-intelligence` 作为安装路径。

也可以直接对 Agent 说：

> 安装 https://github.com/chenergou847/global-policy-intelligence 中的 global-policy-intelligence 子目录，保留完整技能文件夹，安装其 requirements.txt，进入 tools 运行 doctor。不要把外层仓库当作技能根目录。

Python 3.10+。采集使用 HTTPX、lxml、PyYAML、cssselect、pypdf；计划只需要 PyYAML；证据校验只使用 Python 标准库。建议在独立虚拟环境安装依赖。

## 使用

在技能的 `tools/` 目录执行：

```bash
python -m collector doctor
python -m collector collect --dry-run --sources federal_register
python -m collector collect --sources federal_register --max-fetch 10 --evidence /path/to/project/evidence
python -m collector verify /path/to/report.md --claims /path/to/claims.json --evidence /path/to/project/evidence
```

Windows 路径同样支持；输出统一使用 UTF-8。

任务可直接交给 Agent，例如：“分析近三个月欧盟数据规则对我们新加坡托管业务的影响，列出法务行动与待确认项。”简单问题采用短答；完整扫描采用管理层报告；更新任务只分析变化及其影响。

### 信源覆盖

`sources.yaml` 是有限的站点适配器集合，目前以保险、金融议题为主。默认只有美国 Federal Register 与英国立法官方源，**不等于全球政策覆盖**。研究必须先按企业实际法域、业务和时间窗口建立覆盖清单；注册表缺少的法域使用网页工具检索官方原文或补充适配器。

`--sources` 显式选择任何已注册 key，包括 `enabled: false` 的源；未知 key 或空选择返回错误。停用通常意味着未经验证或访问困难，显式选择不改变其可信度。Artemis 为 L3 行业线索，必须显式选择。

网络请求失败、列表无法解析和正文未取得均会记录原因。零命中不能解释为“没有政策动态”。

### 证据与校验

权威层级 `L1-L5`、正文获取状态、法律生效状态、分析置信度分别记录。`full` 是采集器的完整性候选标记，仍需核对正文边界、附件、修订和适用条款。仅启发式 HTML 抽取或混合扫描 PDF 会降级为 `partial`。

严格校验要求 `claims.json` 把报告中的事实、推论、公司信息和假设逐项对应到证据 URL：

```json
{
  "schema_version": 1,
  "claims": [
    {
      "id": "C1",
      "text": "本办法自2026年11月1日起施行。",
      "kind": "fact",
      "importance": "core",
      "evidence": ["https://example.gov/policy/1"]
    }
  ]
}
```

示例 URL 仅展示格式。实际校验必须使用采集并存证的原文 URL。完整契约见 [报告模板](global-policy-intelligence/references/report-template.md)。

核心 `fact/inference` 必须对应可读 `full`、L1/L2 快照。事实的引文、日期、文号、数字只与该项所引证据匹配，不能用另一份无关文件中的相同数字凑过检查。公司自有数据、假设与推论的语义需人工复核。

```bash
# 旧报告迁移时可显式运行兼容检查；不代表严格验收
python -m collector verify report.md --evidence evidence --legacy-checks-only
```

机器检查通过不证明法律适用、因果推论正确，也不能证明正文所有重要结论都已进入台账。交付前仍需检查台账覆盖和推理质量。

## 检查与评估

在仓库根目录安装依赖后：

```bash
python global-policy-intelligence/tools/check.py
```

检查包含原有回归、逐项证据校验、存证完整性、PDF/CSS 解析和本机 HTTP 流水线。GitHub Actions 在 Windows/Linux、Python 3.10/3.12 运行相同检查，不依赖外部站点稳定性。

分析质量评估与程序测试分开：`evals/evals.json` 保留真实任务，`tools/evals/grade_analysis.py` 校验独立评审提交的逐项评分和报告证据。详见 [评估说明](global-policy-intelligence/evals/README.md)。未执行的分析任务不会被计为通过。

## 目录

```text
repository/
  README.md
  .github/workflows/checks.yml
  global-policy-intelligence/       <- 安装这个目录
    SKILL.md
    requirements.txt
    references/
    evals/
    tools/
      collector/
      sources.yaml
      check.py
      tests/
      evals/
```

## 开源组件与边界

[cssselect](https://cssselect.readthedocs.io/) 负责标准 CSS 解析；[pypdf](https://pypdf.readthedocs.io/) 负责 PDF 文字层解析，替代原有自制转换和正则 PDF 解析。[HTTPX](https://www.python-httpx.org/) 负责连接池与环境代理路由。通过依赖调用，不复制第三方源码；第三方许可证见各项目及安装包。

只采集公开页面，不绕过验证码、登录墙或付费墙。内部资料仅在用户授权和工具可用时读取，不得进入外部搜索词。本技能不替代当地专业法律或税务意见。
