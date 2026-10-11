# 分析质量评估

`evals.json` 中的四个 analysis 任务是行为评估题。文件能解析、expectations 数量足够，均不代表模型完成了任务或分析质量通过。

1. 给执行 Agent 提供技能、原始任务及实际可用的证据/工具。在单独目录生成报告、claims ledger 和证据库。记录执行日期、模型与输入。
2. 由独立评审者逐项核对 expectations，填写 pass 与具体理由（报告位置、证据和实际行为）。作者不能用自己的自评替代独立评审。
3. 生成提交格式：`python tools/evals/grade_analysis.py --template`。template 是未评分样例，不算通过。
4. 保存 review JSON，运行 `python tools/evals/grade_analysis.py review.json`。所有四题必须有完整评分与可读取 artifacts；每份报告同时执行严格证据校验。缺题、缺评分、未通过项、缺证据均返回 2。

review JSON 包含 schema_version=1、producer、reviewer 和 cases。每题记录 id、report、claims、evidence 路径（相对 review JSON）与 reviews 数组。reviews 每项包含 expectation（从 1 编号）、pass（布尔值）、rationale（具体评审理由）。

评分脚本只验证评审提交的完整性和证据契约，不能自动证明评审者独立、评分正确、法律适用或推论成立。完整 qualitative eval 应由独立人员/代理实际执行，结论随模型、日期、证据覆盖改变。

`tools/check.py` 运行程序回归与检索流水线评测；其中的测试通过数量不包含未执行的分析质量题。CI 同样不会把这四题的 JSON 结构检查当成分析质量通过。
