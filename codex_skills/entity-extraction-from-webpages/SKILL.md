---
name: entity-extraction-from-webpages
description: 从网页抽取结构化实体与属性。基于本仓库 ingest-pipeline 的 "URL → 抓取 → 分类 → 抽取 → 翻译 → 落库" 链路整理，覆盖 extractor-worker 的 prompt / schema 校验 / 来源归属、scraper-worker 抓取策略、classifier-worker 打小类标签、translator-worker 归一化后处理。Use when Codex 需要为给定网页（URL 或 HTML）按 JSON Schema 抽取实体对象、设计/调试抽取 prompt、调整来源归属策略、规范化实体命名（aliases / original_name / canonical_name），或在 ingest-url 失败时定位 "context/schema/quote/naming" 哪一环出问题。不覆盖：本体 / 概念 / 数据库落库 / 批量 Excel 调度等系统侧逻辑。
metadata:
  short-description: 从网页抽取实体与属性
---

# 从网页抽取实体与属性

本 skill 聚焦 "URL → 实体" 抽取链路中**与抽取本身相关的部分**：抓取策略、上下文预处理、schema 设计与 prompt、LLM 抽取、来源归属、命名字段规范、翻译归一化。它**不**涉及本体 / 概念 / 数据落库 / Excel 调度等系统侧逻辑（要看那些，去看 `ingest-pipeline.service.ts` / `ingestFromExcel` / `OntologyLoaderService`）。

## 0. 全景（30 秒读懂）

```
URL
  → [scraper-worker]   抓取 + 清洗   → html / text / html_cleaned / final_url / attachments
  → [classifier-worker] 多标签分类   → 选中 ontology + concept path 列表
  → [ontology_loader]   本地查库     → 把 concept 合并成 JSON Schema
  → [extractor-worker]  LLM 抽取     → { objects: [{ data, quote, confidence, source }] }
  → [translator-worker] 翻译+归一化  → enum 校正 / 数字串 → int / 罗马→英文序数 / 命名后处理
  → [ingest-pipeline]   落库        → entity.concept_id = leaf concept；entity_type 优先级链
```

## 1. 端到端代码定位

| 阶段 | 文件 | 关键函数 |
|---|---|---|
| 抓取 | `intel-pipeline/workers/scraper-worker/src/scraper_worker/main.py` | `fetch()` / `_clean_html()` / `_clean_html_to_text()` / `_extract_attachments()` / `_extract_metadata()` |
| 分类 | `intel-pipeline/workers/classifier-worker/src/classifier_worker/main.py` | `subject_and_ontology()` / `tag_concepts()` |
| 抽取 | `intel-pipeline/workers/extractor-worker/src/extractor_worker/{main,extract,prompts,html_cleaner}.py` | `extract_from_schema()` / `_validate_object()` / `_match_source()` / `_strip_html_to_text()` / `build_user_prompt()` / `simplify_schema()` |
| 抽取编排 | `playground-sources/backend/src/ingest-pipeline/ingest-pipeline.service.ts` | `ingestUrl()` / `runCrawl()` / `runExtraction()` / `runTranslation()` / `splitForTranslation()` / `persistEntity()` |
| 抽取客户端 | `playground-sources/backend/src/ingest-pipeline/extractor.client.ts` | `extract()` / `extractObjects()` |
| 翻译 | `intel-pipeline/workers/translator-worker/src/translator_worker/{main,translate,prompts}.py` | `translate_json()` / `_normalize_value()` / `_apply_naming_postprocess()` / `normalize_number_with_suffix()` |
| 翻译编排 | `playground-sources/backend/src/ingest-pipeline/translator.client.ts` + `ingest-pipeline.service.ts` 中 `runTranslation()` | `splitForTranslation()` / `shouldSkipTranslation()` |

## 2. context 是怎么来的

抽取质量 80% 取决于 context。context 选取规则在 `runCrawl`（抓取后的 `fetch`）和 `_extract_from_schema_impl`（HTML 二次清洗）中。

### 2.1 抓取阶段（scraper-worker）

调 `scrape::fetch(url, mode?, timeout?, headers?)`，返回结构（关键字段）：

```json
{
  "url": "<入参原始 URL>",
  "status": 200,
  "html": "<原始 HTML>",
  "text": "<lxml 剥 script/style/iframe/svg/... 后拼接的纯文本>",
  "html_cleaned": "<lxml 剥噪声后保留 table/h1-h6/ul/ol/li/dl/dt/dd/a/img[alt] 的 HTML>",
  "metadata": { "title": "...", "description": "...", "og": {...}, "canonical": "...", "lang": "..." },
  "final_url": "<跟随后重定向的最终 URL>",
  "redirect_chain": [...],
  "headers": { "content-type": "...", ... },
  "encoding": "utf-8",
  "content_hash": "sha256-hex-of-html",
  "attachments": [{ "type": "image|video|audio|attachment", "url", "name", "alt" }]
}
```

要点：

- **抓取模式**：`auto`（默认）按 `static (curl_cffi) → dynamic (Playwright) → stealthy (反爬)` 升级；HTTP 4xx/5xx 直接抛不升级。
- **总预算** = `timeout * 3 + 10s`（防 auto 模式死循环）。
- **重试**：网络 / 超时错误用 `tenacity` 重试最多 3 次（指数退避 1/2/4s），HTTP 错误不重试。
- **异常** 自带 `kind`：`network` / `timeout` / `http` / `unknown`，上层据此分类（HTTP 4xx/5xx 通常意味着目标资源没了，重试无意义）。
- **附件过滤**：维基百科 UI 图标（`/static/images/icons/...`、wordmark、favicon、`/thumb/.../<=100px-`、过期国旗）自动跳过，**不**进入 `attachments` 数组。
- **代理**：只读 worker 环境变量 `SCRAPER_PROXY_URL`，payload 显式禁用。

### 2.2 orchestrator 选 context（`ingest-pipeline.service.ts` ingestUrl）

```ts
const context = fetch.html_cleaned
             || fetch.html
             || fetch.text
             || fetch.final_url;
const contextFormat = (html_cleaned || html) ? 'html' : 'text';
```

**优先级**：保留结构的 `html_cleaned` > 原始 `html` > `text` > 最后的兜底 `final_url`。能保住 table / list 结构是核心诉求，LLM 可以从 infobox 抽字段。

### 2.3 extractor 二次清洗（`_extract_from_schema_impl`）

当 `context_format === 'html'` 或文本里出现 `<html`/`<div`/`<p>`，会再走一次 `clean_html_smart`：

- `trafilatura.extract(html, output_format="markdown", include_tables=True, include_images=False, include_links=False)`：识别正文、剥广告 / 导航 / 脚本，保留表格 / 列表 / 代码块 / Wikipedia infobox。
- 再过 `clean_markdown_rules` 清掉维基噪声：`[edit]` / `[citation needed]` / `[1]` / `(June 2013)` / "Click [show] for important translation instructions." / "You can help expand this section." / 多余空行 / 行尾空格。
- 清洗后 `context_format` 强制转 `'text'`（不再是 HTML）。
- Trafilatura 失败时降级返回原 HTML（让上层处理）。

**判断 context 问题的快速清单**：

- LLM 抽出的字段值莫名带 HTML 标签 → context_format 错了或上游没清洗。
- 抽不到 infobox 字段 → 抓取走的是 `text` 路径（`html_cleaned` 缺失）。
- LLM 返回的 quote 跟 context 对不上 → 多半是 Trafilatura 把表格打散或合并了；属正常现象，worker 会用 `_strip_html_to_text` 兜底。

## 3. JSON Schema 设计

Schema 是 LLM 的 "输出合同"。它由 orchestrator 在 Step 3 阶段从 ontology 多 concept 合并生成（`OntologyLoaderService.loadMergedSchema`），但**最终发到 extractor 的 schema 内容**才是 LLM 真正看到的。

### 3.1 prompt 里实际塞给 LLM 的 schema

走的是 `simplify_schema()`（`prompts.py`），不是原 schema。简化策略：

- 保留：`title` / `type` / `required`。
- properties 只留：`type` / `enum` / `items`（数组元素类型）/ 嵌套 `object` 的 `properties`/`required`。
- 移除：`description`（默认）/ `default` / `examples` / `format` / `pattern` 等元数据。
- 嵌套 object 递归简化。

> **含义**：写 schema 时堆 `description` / `examples` / `format` / `pattern` **不会**出现在 LLM 看到的合同里。给 LLM 提示要写到 SYSTEM / USER prompt 的"重要提取指导"里。

### 3.2 prompt 中字段设计的"潜规则"

见 `SYSTEM_PROMPT` / `build_user_prompt` / `prompts.py`。LLM 被硬性约束的语义：

1. **只返回一个对象**（除非 context 提到多个主体，标准 ingest-url 场景下 1 个）。
2. **必填字段**：在 context 存在则填；不存在则置 `null`（**不**整体跳过对象）。`required` 鼓励尽量多填，缺值才 `null`。
3. **`quote`**：必须是 context 中的**精确子串**。worker 用 `_match_source` 在原 context 里 substring 匹配定位。
4. **`original_name`**（如果 schema 声明了）：从原文逐字复制主体名称，**不翻译 / 不转写 / 不加国家 / 厂商 / 编号 / 类别**；多个写法时取最早出现的；找不到置 `null`。
5. **`aliases`**（如果 schema 声明了）：**单一字符串**，用 `、`（中文顿号）/ `,` 分隔多个别名。支持的类型（按惯例）：designations / short / local / code / historical / translation / official。要**去重**且**不与 name / canonical_name 重复**。
6. **`name` 和 `canonical_name`**：抽取阶段**不要生成**，留空（命名规整交给落库阶段，orchestrator 有自己的优先级链）。
7. **HTML context 偏好**：优先从信息框 / 数据表 / 规范列表 / 标题抽；保留引号里有用的转换值；除非 schema 明确要求，否则不要把 `href` 当字段值。
8. **多实体场景**：抽取始终只挑"最佳目标主体"，不要把背景实体也抽出来当独立对象。

### 3.3 字段类型 vs translator 行为

schema 字段类型在 translator 阶段会被 `_normalize_value` 严格归一化：

| 字段类型 | translator 行为 | 设计建议 |
|---|---|---|
| `string` | 强转 str；dict/list 强转 `json.dumps` | 自由文本、人物名、机构名 |
| `integer` / `number` | 字符串去千分位后正则；"12.0" → 12 | 数量、年代、长度 |
| `boolean` | 接受 `"true"/"yes"/"1"/"是"/"真"` 等 | 标志位 |
| `array` | 字符串按 `,;\n\|` 切分；或尝试 JSON parse | 列表型 |
| `object` | 补 default；丢未知字段（看 `drop_unknown_fields`） | 嵌套结构 |
| `enum` | 大小写不敏感匹配 | 固定值域，**保留原 schema 写法** |

**机器标识字段**（`shouldSkipTranslation`）一律不送 translator：number/integer/boolean、enum、`original_name` / `identifier` / `gjb_code` / `gjb_path` / `standard_source` / `country_code` / `iso_code` / `iso_country_code` / 任何 `*_code` / `*_id` / `*_ids` / `*_ref` / `*_url` / `*_href` / `*_uri` / `mapping_*` / `legacy_*` / `source_*` / `model_id` / `series_id`。**命名新字段时按这个表走**。

## 4. 实战：JSON Schema 模板

下面是一个抽取"装备型实体"用的典型 schema（业务可直接套用）：

```json
{
  "title": "Equipment",
  "type": "object",
  "required": ["original_name", "aliases"],
  "properties": {
    "original_name":   { "type": "string", "description": "原文逐字复制的官方名称" },
    "aliases":         { "type": "string", "description": "用、号分隔的别名集合" },
    "designer":        { "type": "string", "description": "设计方 / 厂商" },
    "manufacturer":    { "type": "string", "description": "制造商" },
    "country_code":    { "type": "string", "description": "ISO 3166-1 alpha-2 / alpha-3", "enum": ["USA", "RUS", "CHN", "FRA", "GBR", "DEU"] },
    "in_service_since": { "type": "integer", "description": "入役年份" },
    "in_service_until": { "type": "integer", "description": "退役年份（仍在役置 null）" },
    "type":            { "type": "string", "enum": ["fighter", "bomber", "transport", "helicopter", "uav"] },
    "description":     { "type": "string", "description": "≤200 字中文概述" },
    "specs": {
      "type": "object",
      "properties": {
        "max_speed_kmh":  { "type": "number" },
        "range_km":       { "type": "number" },
        "crew":           { "type": "integer" },
        "length_m":       { "type": "number" }
      }
    }
  }
}
```

要点对照 §3：

- `original_name` 标了 schema → 强制 LLM 必填（系统提示里写的）。
- `aliases` 标了 schema → 走"顿号分隔 + 类型分类 + 去重"的规则。
- `country_code` 用 `enum` 限制 → 落库时直接保留原写法。
- `in_service_since/until` 用 `integer` → 翻译时自动把 "1991" 强转 int。
- `specs` 嵌套 object → translator 会按子 schema 递归归一化。
- 故意没加 `name` / `canonical_name` → 留给落库阶段决定（`persistEntity` 的优先级链）。

## 5. 抽取阶段的代码骨架

抽取逻辑全部在 `extract_from_schema` → `_extract_from_schema_impl`：

```
payload = { schema, context, context_format, schema_id, subject, hints, options }
  ↓
context_format 校验（必须是 'html' 或 'text'）
  ↓
如果是 html / 含标签：clean_html_smart → format 强转 'text'
  ↓
max_objects = options.max_objects ?? 100
strict      = options.strict ?? True（ingestUrl 用 false）
  ↓
build_user_prompt(schema, context, context_format, subject, hints, options)
  ↓
client.chat_text(messages, response_format={"type": "json_object"})
  ↓
_safe_json_loads（剥 markdown fence / 取首个 {…} / […]）
  ↓
对每个 raw object：
    _validate_object(data, schema)  // 必填 + 类型
        strict 失败 → 丢弃
        非 strict → 保留（仅 warning）
    _match_source(quote, context, context_format)
        → { matched, context_snippet }
    组装：
        {
          id: "obj_<uuid12>",
          type: schema.title,
          data,
          source: { url, context_format, context_snippet, quote },
          confidence: float 截断 [0, 1]
        }
  ↓
返回 { schema_id, objects[], stats: { raw_objects, extracted_objects, invalid_objects, schema_valid, elapsed_ms, model, … } }
```

注意：返回的每个 object `id` 是 worker 内部 uuid（`obj_<hex12>`），**不**是数据库 `entity_id`。落库后才会拿到真正的 `entity_id`。

## 6. 来源归属：`_match_source` 行为

目的：把 LLM 给的 `quote` 定位回 context 里的实际出处，写到 `object.source.context_snippet`。

行为（按顺序）：

1. 直接 `quote in context` → `matched=true`，snippet 就是 quote 本身。
2. context 是 HTML 时：先 `_strip_html_to_text(quote)`（剥标签 + 解 HTML 实体），再 `in context_text`。
3. 都不匹配 → `matched=false`，`context_snippet=""`。

这是**子串匹配**，不是相似度匹配。因此：

- LLM 给出 "B-2 Spirit" 不会去匹配 "B 2 Spirit"（中间空格变短横线这种属于 LLM 自己的处理）。
- 如果你看到 `matched=false` 很多 → 多半是 Trafilatura 把表格单元格合并 / 拆开了，或 LLM 把句子轻微改写了。

## 7. 翻译 / 归一化：translator-worker

`runTranslation()`（`ingest-pipeline.service.ts`）的拆字段逻辑：

1. `splitForTranslation(data, schema)`：按 schema 拆成 `passthrough`（机器标识字段，不进 LLM）和 `toTranslate`（其余文本字段）。
2. 如果 `toTranslate` 空 → 跳过 worker，直接返回原 data。
3. 调 `translate::json`，options = `{ strict: true, preserve_nulls: true, drop_unknown_fields: false }`。
4. 合并：`merged = { ...passthrough, ...translatedData }`，translator 没返回的字段从 `toTranslate` 原值兜底（防字段被吞）。
5. 失败 → warning + 用原 data 落库。

worker 内部 `_normalize_value` 做的事（**不是**翻译，是矫正）：

- enum 匹配（大小写不敏感）
- boolean / integer / number 字符串强转
- array 字符串切分或 JSON parse
- object 补 default / 丢未知字段（`drop_unknown_fields=true` 时）

`_apply_naming_postprocess`（仅 schema 含 `name` / `canonical_name` / `original_name` / `aliases` 时触发）：

- `*_code` / `*_id` / `*_url` / `original_name` / `canonical_name` / `code` / `country_code` / `iso_code` / `gjb_code` / `gjb_path` / `standard_source` 等 → **透传不处理**。
- `designation` / `model_number` / `series` / `variant` / `block` / `mark` / `mod` → 仅做数字 / 序数规整（罗马 / 中文 → 英文序数："第十二" → "12th"、"XII" → "12th"）。
- `name` 字段 → 数字后缀规整 + 去停用词（英文 `the`/`a`/`an`、中文 `号`/`级`/`系`）。

→ 设计 schema 时，**命名相关字段尽量直接对应这些 key** 才能让后处理生效。

## 8. 调试与排错

### 8.1 开启 dump 文件

设环境变量 `INGEST_DEBUG_DIR`（任意非空值），每次抽取 / 翻译会写：

- `<DEBUG_DIR>/extract-<ts>-<sha1short>.json`：`request_url` / `schema_id` / `schema` / `subject` / `hints` / `options` / `context`（截断到 `DEBUG_CONTEXT_MAX`） / `response` / `error`。
- `<DEBUG_DIR>/translate-<ts>-<sha1short>.json`：同上但 translator 视角。

这是核对"LLM 究竟看到了什么、返回了什么"的最直接手段。

### 8.2 常见失败模式 & 定位

| 现象 | 根因 | 怎么定位 |
|---|---|---|
| `extractor` 503 / 调用失败 | extractor-worker iii client 未 ready | 看 service 日志 `extractor iii client 初始化失败`；检查 `III_URL` |
| objects 全空 | LLM 判定 context 不含目标主体 | 看 extract dump 里的 `context` 与 `response.objects`；context 是不是被噪声吃了 |
| 字段值带 HTML 标签 | context_format 选错 | 看 `context_format` 是否真的是 'html' 而非 'text' |
| 必填字段全是 null | context 缺信息 或 LLM 严格模式被触发 | 调 schema 的 `required` 列表；或 ingestUrl 端用 `strict=false` 兜底（已默认） |
| quote 全 `matched=false` | Trafilatura 把表格打散 / 合并 | 接受；只是来源归属信号弱，**不影响抽取本身** |
| 抽出来的名称很奇怪（带厂商 / 编号） | LLM 没遵守 `original_name` 规则 | 看 SYSTEM_PROMPT 是否被覆盖；减少 schema 里的 description 干扰 |
| translator 字段值被改 | 字段名命中 `shouldSkipTranslation` 黑名单 | 改字段名（去掉 `_code` / `_id` 等后缀）或显式用 `type: number/integer/boolean` |
| 落库的 entity_type 总是 'unknown' | LLM 没填 `entity_type`，且 leaf concept 也没设 | 优先在 schema 显式声明 `entity_type` 字段 |

### 8.3 抽 1 条 URL 跑端到端的最快方法

```bash
curl -X POST http://localhost:3200/api/ingest-pipeline/ingest-url \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://en.wikipedia.org/wiki/B-2_Spirit",
    "persist_document": true,
    "persist_entities": true,
    "persist_translated": true
  }'
```

想要"只看不写库"：把 `persist_document` / `persist_entities` / `persist_translated` 全设 `false`。

想要"指定主体跳过分类"：

```json
{ "url": "https://...", "subject": "B-2 Spirit" }
```

想要"指定本体跳过选大类"：用 `POST /ingest-pipeline/ingest-url-local-with-ontology`，带 `ontology_id`。

## 9. 修改抽取行为时的检查清单

按这个顺序检查，能快速定位"我的抽取为什么没按预期工作"：

1. **context 是否对**：抓 mode (`auto` / `static` / `dynamic` / `stealthy`)，抓回来后 `html_cleaned` 是否非空，context_format 是否被识别成 'html'，二次清洗后是否还残留 HTML 标签。
2. **schema 是否对**：把 `simplify_schema(schema)` 跑一遍看看实际进 prompt 的内容；`description` / `examples` / `format` 不会进 prompt。
3. **必填是否对**：业务侧要求的必填字段是否写在 `required` 里；该字段在 context 里是否有可信源。
4. **subject 是否干扰**：传了 `body.subject` 会让 LLM 只抽该主体的信息；不传则 LLM 自己识别主体（可能抽错主体）。
5. **hints.tag_paths 是否对**：分类阶段给的 concept path 会作为 LLM 的方向提示；不传或传空 → 提示缺失。
6. **strict 模式**：ingestUrl 默认 `strict=false`，业务自己用 `extract::from_schema` 时记得显式传。
7. **translator 字段名**：避免 `*_code` / `*_id` / `*_url` 等机器标识字段被吃掉；命名相关字段用 `name` / `canonical_name` / `original_name` / `aliases`。
8. **dump 文件**：设 `INGEST_DEBUG_DIR` 后看 extract-* / translate-* 文件，对比 request / response。

## 附录 A. 实战补充

### A.1 嵌套 schema 在 prompt 里被简化成什么样

原始 schema（用户在前端定义）：

```json
{
  "title": "Tank",
  "type": "object",
  "required": ["original_name"],
  "properties": {
    "original_name": { "type": "string", "description": "官方名（原文）" },
    "aliases":       { "type": "string", "description": "别名（顿号分隔）" },
    "type":          { "type": "string", "enum": ["MBT", "light", "heavy"], "default": "MBT" },
    "specs": {
      "type": "object",
      "description": "技术参数",
      "properties": {
        "main_gun_mm":  { "type": "number", "description": "主炮口径（mm）", "minimum": 0 },
        "crew":         { "type": "integer", "description": "车组人数" },
        "fire_control": { "type": "string", "enum": ["hunter-killer", "stabilized"] }
      },
      "required": ["main_gun_mm"]
    }
  }
}
```

进 prompt 的 `simplify_schema` 后：

```json
{
  "title": "Tank",
  "type": "object",
  "required": ["original_name"],
  "properties": {
    "original_name": { "type": "string" },
    "aliases":       { "type": "string" },
    "type":          { "type": "string", "enum": ["MBT", "light", "heavy"] },
    "specs": {
      "type": "object",
      "properties": {
        "main_gun_mm":  { "type": "number" },
        "crew":         { "type": "integer" },
        "fire_control": { "type": "string", "enum": ["hunter-killer", "stabilized"] }
      },
      "required": ["main_gun_mm"]
    }
  }
}
```

→ LLM 看不到 `description` / `default` / `minimum` / `pattern` 字段；想给 LLM 提示就写进 SYSTEM / USER prompt 的"重要提取指导"里，不要堆在 schema 上。

### A.2 必填字段的"软强制"

`required` 在 LLM 看来是"尽量填，缺值置 null"；`_validate_object` 也只把"必填字段是 null / 空串 / 空数组"视为失败。

如果业务真的强制要求某些字段必须有值（否则丢弃整条），在 orchestrator 端做：

```ts
if (body.persist_entities) {
  for (const obj of translatedObjects) {
    const missing = composedSchema.required.filter(k =>
      obj.data[k] == null || obj.data[k] === ''
    );
    if (missing.length > 0) {
      warnings.push(`缺少必填 ${missing.join(',')}，跳过 entity ${obj.id}`);
      continue;
    }
    const row = await this.persistEntity(...);
  }
}
```

### A.3 context_format 误判

如果上游 scraper 返回了 `text`（即 `html_cleaned` 和 `html` 都为空），orchestrator 不会传 `html` 给 extractor。但如果文本里意外残留了 `<div>` 等标签，`_extract_from_schema_impl` 会**自动**走 `clean_html_smart` 一次。这通常无害，但会**再消耗一次 Trafilatura 调用**。如果想强制走 `text`，把 context 里残留的标签去干净。

### A.4 strict 模式取舍

- ingestUrl 用 `strict: false`：LLM 输出的字段即使类型不对也保留，让 translator 兜底。**建议业务自己实现时也用 false**，避免一次抽取全军覆没。
- 走 `extract::batch` 时每条 task 独立 strict，失败记入 `errors` 不影响其他。

### A.5 抽取量上限

`options.max_objects` 默认 100，但 `ingestUrl` 走"单 URL 单目标"路径，prompt 里硬约束"只返回一个对象"，worker 实际拿到的也就是 1~2 个。`max_objects` 主要给 `extract::batch` 兜底。

### A.6 异常 kind 与重试策略

extractor-client 调 `extract::from_schema`，iii trigger 默认 180s 超时（`INGEST_EXTRACTOR_TIMEOUT_MS` 可改）。worker 内 LLM 调用的异常归类：

- `network` / `timeout` → 可重试（但当前 ingestUrl 没加重试，需要的话在 `runExtraction` 套一层）。
- `http`（LLM 4xx/5xx）→ 看 status：401/403 检查 API key；429 退避。
- `parse`（LLM 返回非 JSON）→ 多半是 prompt 太长 / model 抽风，可重试一次。

### A.7 attachments 与 entity 无关

scraper 抓回的 `attachments` 数组是给 `source_documents` / `document_archives` 落库用的（图片 / 视频 / 文档 / 音频），**不**进 LLM 抽取 context。LLM 只看 `html_cleaned` / `html` / `text`。