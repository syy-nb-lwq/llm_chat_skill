# 抽取代码调用栈速查

按抽取 URL 的执行顺序，每一步标注关键文件 + 关键函数 + 关键行为。可以直接跳读对应行。

## 1. 抓取（scraper-worker）

- `intel-pipeline/workers/scraper-worker/src/scraper_worker/main.py`
  - `main()`：注册 `scrape::fetch` 和 `scrape::extract` 两个 function。
  - `fetch(payload, worker)`：入参 `{url, mode?, timeout?, headers?}`，出参见 SKILL.md §2.1。
    - 模式策略：
      - `auto` → `_fetch_with_retry` 顺序试 `static → dynamic → stealthy`。
      - 指定 mode → 直接走对应 fetcher。
    - 总预算 = `timeout * 3 + 10s`。
    - 重试装饰器 `_RETRY`：`NetworkError` / `TimeoutError` 重试 3 次，指数退避，HTTP 4xx/5xx 不重试。
  - `_clean_html(html)`：lxml 剥 script/style/iframe/svg/... / 解 HTML 注释 / 删 on*/style/data-* 属性，**保留** table/h1-h6/ul/ol/li/dl/dt/dd/a/img[alt]。这是 `html_cleaned` 字段。
  - `_clean_html_to_text(html)`：在 `_clean_html` 的基础上进一步剥所有标签，规范化空白。这是 `text` 字段。
  - `_extract_attachments(html, base_url)`：扫 img/video/audio/a[href]，按后缀分类为 image/video/audio/attachment；过滤维基百科 UI 图标（`_is_wikipedia_ui_icon`）。

## 2. orchestrator 选 context（NestJS）

- `playground-sources/backend/src/ingest-pipeline/ingest-pipeline.service.ts` — `ingestUrl()`（line 174）
  - `runCrawl(body)`（line 668）：
    1. `this.crawl.scrapeOnly({url, mode, timeout, headers})` → 走 `scrape::fetch` iii trigger。
    2. 可选 `dataAssets.upsertSourceDocument` + `createDocumentArchive`。
  - 主流程选 context（line ~213）：
    ```ts
    const context = fetch.html_cleaned || fetch.html || fetch.text || fetch.final_url;
    const contextFormat = (html_cleaned || html) ? 'html' : 'text';
    ```

## 3. 抽取（extractor-worker）

- `intel-pipeline/workers/extractor-worker/src/extractor_worker/main.py`
  - `main()`：注册 `extract::from_schema` 和 `extract::batch`。
- `extract.py` — `extract_from_schema()`（line ~270 起）
  - `_extract_from_schema_impl(payload)`：
    1. context_format 校验。
    2. 若是 HTML 或文本里有 `<html`/`<div`/`<p>` → `clean_html_smart` → format 强转 'text'。
    3. `build_user_prompt(...)` 构造 user 消息。
    4. `client.chat_text(..., response_format={"type": "json_object"})`。
    5. `_safe_json_loads` 容错解析。
    6. 对每个 raw object：
       - `_validate_object(data, schema)`：必填字段 + 简单类型校验（line 150）。
       - `_match_source(quote, context, context_format)`：substring 匹配（line 213）。
       - 组装 `{id, type, data, source, confidence}`。
  - `extract_batch(payload)`：顺序跑，失败记 `errors[]`，成功进 `results[]`。
- `prompts.py`
  - `simplify_schema(schema, remove_descriptions=True)`（line 13）：递归剥 description/default/examples/format/pattern。
  - `SYSTEM_PROMPT`：硬约束"只返回一个对象"、字段语义、HTML 上下文偏好、aliases / original_name 规则。
  - `build_user_prompt(...)`（line 95）：拼装 user 消息（schema / hints / subject / context / 重要提取指导）。
  - `build_response_schema(max_objects=100)`：始终返回 `{"type": "json_object"}`（max_objects 在 prompt 端没用，只用于 LLM response_format；实际单目标只 1 个 object）。
- `html_cleaner.py`
  - `clean_html_smart(html, url, use_rules_cleanup=True)`（line 79）：Trafilatura + 维基规则清噪。

## 4. 抽取编排（NestJS）

- `playground-sources/backend/src/ingest-pipeline/ingest-pipeline.service.ts` — `runExtraction()`（line 1026）
  - 调 `this.extractor.extract({schema, schema_id, context, context_format, subject, hints, options})`。
  - strict=false，options.url=fetch.final_url。
  - 失败 → `warnings.push("抽取失败: …")` 并把请求/响应写到 `INGEST_DEBUG_DIR`（如果设置了）。
- `extractor.client.ts` — `extract()` 调 iii trigger `extract::from_schema`，默认 180s 超时。

## 5. 翻译 / 归一化（translator-worker）

- `intel-pipeline/workers/translator-worker/src/translator_worker/main.py`
  - `main()`：注册 `translate::json` 和 `translate::batch`。
- `translate.py` — `translate_json()`（line ~470 起）
  - `_translate_with_llm(data, schema, schema_id, options)`：调 LLM，return (data, model, raw_content)。
  - `_normalize_value(value, schema, path, options, issues)`：按 schema 递归归一化（line 448）。包括 enum/bool/int/number/string/array/object 的强转。
  - `_apply_naming_postprocess(value, options)`（line 609）：仅当 schema 含 name/canonical_name/original_name/aliases 时触发。罗马 / 中文序数 → 英文序数（"第十二" → "12th"）；name 字段去停用词。
- `prompts.py`：翻译 prompt（详见 translate-worker 源码；本 skill 不展开）。

## 6. 翻译编排（NestJS）

- `playground-sources/backend/src/ingest-pipeline/ingest-pipeline.service.ts` — `runTranslation()`（line 1246）
  - `splitForTranslation(data, schema)`：按 schema 拆 passthrough / toTranslate。
  - toTranslate 空 → 跳过 worker，直接返回原 data。
  - 调 `this.translator.translate(...)`，options = `{strict: true, preserve_nulls: true, drop_unknown_fields: false}`。
  - 合并：`merged = { ...passthrough, ...translatedData }`；missingFromResponse 的字段从 toTranslate 兜底。
  - 失败 → warning + fallback 原 data。
- `shouldSkipTranslation(key, propDef)`（line ~1185）：黑名单见 SKILL.md §3.3。

## 7. 落库（本 skill 不展开）

- `persistEntity()`（line ~1350）：构造 `entity_type` / `canonical_name` 优先级链，调 `dataAssets.createEntity`。
- `dataAssets.createEntity(...)`：写入 `entities` 表。

## 8. 调试 dump

设 `INGEST_DEBUG_DIR=<path>`：
- `runExtraction` 调 `dumpExtraction(...)` → 写 `<DEBUG_DIR>/extract-<ts>-<sha1short>.json`。
- `runTranslation` 调 `dumpTranslation(...)` → 写 `<DEBUG_DIR>/translate-<ts>-<sha1short>.json`。

context 默认截断到 `DEBUG_CONTEXT_MAX` 字符（看 service 顶部常量定义）。