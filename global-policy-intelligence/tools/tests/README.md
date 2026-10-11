# tests · 离线单元测试

    python run_tests.py

100 项断言，覆盖的都是「会导致幻觉或漏抓」的真实失败模式：列表解析、正文抽取、
四态判定、编码、PDF、去重、幻觉校验 C1–C4、依赖缺失行为。

无需 pytest（只用标准库），也无需联网。

## 关于 fixtures 与 evals/site 的分工（不要合并）

`tests/fixtures/` 和 `tools/evals/site/` 都放了"同一种页面形态"的样例文件，
看起来像重复，但**不是**——我逐对做过哈希比对，没有一对是字节相同的：

| tests/fixtures | 用途 | evals/site | 用途 |
|---|---|---|---|
| `detail_gov.html` | 单测：正文选择器与元数据抽取 | `static/detail_ok.html` | 评测：完整流水线的 full 路径 |
| `detail_cloudflare.html` | 单测：验证页识别 | `static/detail_cf.html` | 评测：blocked 路径 |
| `detail_js_only.html` | 单测：JS 骨架识别 | `static/detail_js.html` | 评测：JS 骨架路径 |
| `listing_gov.html` | 单测：列表解析 + link_pattern | `static/listing.html` | 评测：多源列表 |
| `nfra_list.json` / `nfra_detail.json` | 单测：JSON 字段映射 | `api/list.json` / `api/detail_1001.json` | 评测：接口源全链路 |

**刻意保持分离的理由**：

1. **改动隔离**——调单元测试夹具不会动到评测基线，反之亦然；
2. **职责不同**——单测夹具追求"覆盖单个解析分支"，评测夹具追求"跑通完整七层流水线、
   并覆盖五种 fetch_status 结果"；
3. **合并的收益只是省几 KB**，代价是两份测试互相牵制、改一处两边都可能红。
   （小文件合计约 13 KB，不值得为此增加耦合。）

`static/rule.pdf` 是由 `evals/make_pdf_fixture.py` 生成的 PDF 夹具，
因为入库了，改生成逻辑后要重跑一次该脚本。
