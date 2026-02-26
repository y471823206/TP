# 翻译词校验工具（设计 + 可运行原型）

该仓库提供一个可落地的「翻译词校验工具」方案与 Python CLI 原型，目标是对海外站搜索词表进行批量校验与纠错，输出新版本词表。

## 目标

- 输入：旧版翻译词表（CSV）
- 流程：
  1. 使用 Hunyuan-MT 重新翻译原词到中文
  2. 使用 LLM 做异常检查、歧义标注、场景适配判断
  3. 结合规则打分，决定保留旧译文 / 使用新译文 / 人工复核
- 输出：
  - `validated_terms.csv`（新版本词表）
  - `review_queue.csv`（人工复核队列）
  - `metrics.json`（质量与成本统计）

## 目录

- `tool/validator.py`：CLI 主程序（可运行原型）
- `tool/sample_terms_backend.csv`：后台词表输入示例
- `tool/sample_terms_search_log.csv`：搜索日志输入示例
- `tool/prompt_template.md`：LLM 评审提示词模板

## 快速开始

### 方式 A：本地 Hunyuan（推荐，无需 MT API）

```bash
python3 tool/validator.py \
  --input tool/sample_terms_backend.csv \
  --output-dir out \
  --hunyuan-mode local \
  --hunyuan-local-cmd 'python3 local_mt.py --lang "{source_lang}" --text "{source_term}"' \
  --llm-endpoint https://api.example.com/llm \
  --llm-token yyy
```

`--hunyuan-local-cmd` 需要输出：
- 纯文本翻译结果（stdout）或
- JSON：`{"translation": "..."}`

### 方式 B：Hunyuan API（可选）

```bash
python3 tool/validator.py \
  --input tool/sample_terms_backend.csv \
  --output-dir out \
  --hunyuan-mode api \
  --hunyuan-endpoint https://api.example.com/hunyuan-mt \
  --hunyuan-token xxx \
  --llm-endpoint https://api.example.com/llm \
  --llm-token yyy
```

> 默认会对每条记录调用一次 MT；仅在“疑似异常/高价值词”触发 LLM，控制成本。

## 输入 CSV 规范（支持两种）

### 1) 后台维护的翻译词表

最少字段：

- `source_term`
- `old_zh`（可用别名 `translation`）
- `category`

可选字段：

- `term_id`（缺失时自动生成 `row_行号`）
- `source_lang`（缺失默认 `auto`）
- `search_freq`（缺失默认 9999）

### 2) 实际搜索日志翻译数据

推荐字段：

- `source_lang`：语种（en/ja/ko/es/...）
- `source_term`（可用别名 `source_text`）：原词
- `old_zh`（可用别名 `translation`）：当前中文译文
- `category`：类目（3d_model/material/texture/su/cad/inspiration/...）
- `search_freq`：搜索频次（整数）

## 校验逻辑概览

1. **基础标准化**：大小写、空格、括号统一。
2. **MT 重译**：调用 Hunyuan-MT（本地或 API）得到 `mt_zh`。
3. **快速规则打分**（低成本）：
   - 字符级相似度
   - 高频词/低相似触发条件
4. **LLM 深度评审**（按阈值触发）：
   - 判断旧译文与 MT 译文谁更贴近“设计师搜索语境”
   - 输出歧义标签、风险等级、建议译文
5. **决策输出**：
   - `accept_old`：保留旧译文
   - `replace_with_mt`：替换为 MT/LLM 推荐译文
   - `human_review`：进入人工队列

## 成本与性能策略

- **分层调用**：先规则、后 LLM，减少 LLM 调用量。
- **高频优先**：按 `search_freq` 优先处理高价值词。
- **缓存**：对相同 `(source_lang, source_term)` 结果缓存，避免重复调用。
- **批处理**：按批次异步调用外部 API，控制 QPS。

## 建议上线方式

- 离线日/周批处理产出新词表。
- 线上搜索仍只查词表，不引入同步耗时。
- 通过 A/B 对比“无结果率、点击率、下载转化率”。
