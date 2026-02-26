#!/usr/bin/env python3
"""翻译词校验工具原型。

输入翻译词表，调用 MT + LLM（可选）进行校验，输出新词表、人工复核队列和统计。
支持两种常见输入：
1) 后台翻译词表：source_term, old_zh, category
2) 搜索日志词表：source_lang, source_term, old_zh, category, search_freq
并兼容别名字段：source_text/source_term、translation/old_zh。

Hunyuan-MT 支持两种调用方式：
- api：HTTP API
- local：本地命令行（开源模型本地推理）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
from urllib import request


CANONICAL_OUTPUT_FIELDS = [
    "term_id",
    "source_lang",
    "source_term",
    "old_zh",
    "mt_zh",
    "final_zh",
    "action",
    "reason",
    "ambiguity_tags",
    "score",
    "category",
    "search_freq",
]


@dataclass
class ValidationResult:
    term_id: str
    source_lang: str
    source_term: str
    old_zh: str
    mt_zh: str
    final_zh: str
    action: str
    reason: str
    ambiguity_tags: str
    score: float
    category: str
    search_freq: int


def normalize_text(text: str) -> str:
    t = str(text).strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.replace("（", "(").replace("）", ")")
    return t


def parse_int(value: str, default: int = 0) -> int:
    v = str(value).strip()
    if not v:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def should_trigger_llm(sim: float, freq: int) -> bool:
    return freq >= 100 or sim < 0.35


def http_post_json(url: str, token: str, payload: dict, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def call_hunyuan_mt_api(endpoint: str, token: str, source_lang: str, source_term: str) -> str:
    payload = {
        "source_lang": source_lang,
        "target_lang": "zh",
        "text": source_term,
    }
    data = http_post_json(endpoint, token, payload)
    return str(data.get("translation", "")).strip()


def call_hunyuan_mt_local(local_cmd_template: str, source_lang: str, source_term: str) -> str:
    cmd = local_cmd_template.format(source_lang=source_lang, source_term=source_term)
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"本地 Hunyuan 命令失败: {proc.stderr.strip()}")

    output = proc.stdout.strip()
    if not output:
        raise RuntimeError("本地 Hunyuan 命令返回为空")

    # 兼容直接输出翻译文本，或输出 JSON: {"translation": "..."}
    try:
        maybe_json = json.loads(output)
        if isinstance(maybe_json, dict) and "translation" in maybe_json:
            return str(maybe_json["translation"]).strip()
    except json.JSONDecodeError:
        pass
    return output


def build_llm_prompt(source_lang: str, source_term: str, old_zh: str, mt_zh: str, category: str) -> str:
    return (
        "你是设计搜索语义专家。请判断哪个中文译文更适合设计师检索场景。\\n"
        f"原语言: {source_lang}\\n"
        f"原词: {source_term}\\n"
        f"旧译文: {old_zh}\\n"
        f"新译文(MT): {mt_zh}\\n"
        f"类目: {category}\\n\\n"
        "请输出 JSON，字段: winner(old|mt|manual), recommendation, reason, ambiguity_tags(数组), risk(low|mid|high)。"
    )


def call_llm(endpoint: str, token: str, prompt: str) -> dict:
    payload = {"prompt": prompt, "temperature": 0.1}
    data = http_post_json(endpoint, token, payload)
    if isinstance(data, dict) and "winner" in data:
        return data
    text = data.get("text", "{}") if isinstance(data, dict) else "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "winner": "manual",
            "recommendation": "",
            "reason": "LLM响应非JSON",
            "ambiguity_tags": ["parse_error"],
            "risk": "high",
        }


def cache_key(source_lang: str, source_term: str) -> str:
    raw = f"{source_lang}||{source_term}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def detect_schema(headers: List[str]) -> str:
    hs = set(headers)
    has_source = "source_term" in hs or "source_text" in hs
    has_old = "old_zh" in hs or "translation" in hs
    has_cat = "category" in hs
    if not (has_source and has_old and has_cat):
        raise ValueError("输入 CSV 至少要包含 source_term/source_text, old_zh/translation, category 三类字段。")

    if "search_freq" in hs or "source_lang" in hs:
        return "search_log"
    return "backend_table"


def canonicalize_row(raw: dict, idx: int) -> dict:
    source_term = str(raw.get("source_term") or raw.get("source_text") or "").strip()
    old_zh = str(raw.get("old_zh") or raw.get("translation") or "").strip()
    category = str(raw.get("category") or "").strip()
    source_lang = normalize_text(raw.get("source_lang") or "auto") or "auto"
    search_freq = parse_int(raw.get("search_freq", "9999"), 9999)

    term_id = str(raw.get("term_id") or f"row_{idx}")
    if not source_term:
        raise ValueError(f"第 {idx} 行缺少 source_term/source_text")

    return {
        "term_id": term_id,
        "source_lang": source_lang,
        "source_term": source_term,
        "old_zh": old_zh,
        "category": category,
        "search_freq": search_freq,
    }


def load_csv(path: Path) -> List[dict]:
    for enc in ("utf-8-sig", "gbk", "gb2312", "latin-1"):
        try:
            with path.open("r", encoding=enc, newline="") as f:
                f.read(1024)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        enc = "utf-8-sig"
    with path.open("r", encoding=enc, newline="") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        if not headers:
            raise ValueError("输入 CSV 缺少表头")

        detect_schema(headers)
        rows: List[dict] = []
        for i, raw in enumerate(reader, start=1):
            rows.append(canonicalize_row(raw, i))
        return rows


def save_csv(path: Path, rows: List[dict], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="翻译词校验工具")
    p.add_argument("--input", required=True, help="输入翻译词表 CSV")
    p.add_argument("--output-dir", required=True, help="输出目录")

    p.add_argument("--hunyuan-mode", choices=["local", "api"], default="local", help="Hunyuan 调用方式")
    p.add_argument("--hunyuan-local-cmd", default="", help="本地命令模板，支持 {source_lang} {source_term}")
    p.add_argument("--hunyuan-endpoint", default="", help="Hunyuan API 地址（mode=api 时必填）")
    p.add_argument("--hunyuan-token", default=os.getenv("HUNYUAN_TOKEN", ""))

    p.add_argument("--llm-endpoint", required=True)
    p.add_argument("--llm-token", default=os.getenv("LLM_TOKEN", ""))
    p.add_argument("--cache", default=".validator_cache.json", help="缓存文件路径")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.hunyuan_mode == "api" and not args.hunyuan_endpoint:
        raise ValueError("当 --hunyuan-mode=api 时，必须提供 --hunyuan-endpoint")
    if args.hunyuan_mode == "local" and not args.hunyuan_local_cmd:
        raise ValueError("当 --hunyuan-mode=local 时，必须提供 --hunyuan-local-cmd")

    input_path = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_csv(input_path)
    cache_path = Path(args.cache)
    cache: Dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    result_rows: List[dict] = []
    review_rows: List[dict] = []
    llm_calls = 0
    mt_calls = 0
    start = time.time()

    for row in rows:
        term_id = str(row["term_id"])
        source_lang = normalize_text(row["source_lang"])
        source_term = str(row["source_term"]).strip()
        old_zh = str(row["old_zh"]).strip()
        category = normalize_text(row["category"])
        freq = parse_int(str(row["search_freq"]), 9999)

        key = cache_key(source_lang, source_term)
        if key in cache:
            mt_zh = cache[key]
        else:
            if args.hunyuan_mode == "api":
                mt_zh = call_hunyuan_mt_api(args.hunyuan_endpoint, args.hunyuan_token, source_lang, source_term)
            else:
                mt_zh = call_hunyuan_mt_local(args.hunyuan_local_cmd, source_lang, source_term)
            cache[key] = mt_zh
            mt_calls += 1

        sim = similarity(normalize_text(old_zh), normalize_text(mt_zh))
        action = "accept_old"
        final_zh = old_zh
        reason = "规则判定：旧译文与MT接近"
        ambiguity_tags = ""
        score = sim

        if should_trigger_llm(sim, freq):
            llm_calls += 1
            prompt = build_llm_prompt(source_lang, source_term, old_zh, mt_zh, category)
            llm = call_llm(args.llm_endpoint, args.llm_token, prompt)
            winner = llm.get("winner", "manual")
            recommendation = str(llm.get("recommendation", "")).strip()
            reason = str(llm.get("reason", "")) or "LLM判定"
            ambiguity_tags = ",".join(llm.get("ambiguity_tags", []))

            if winner == "old":
                action = "accept_old"
                final_zh = old_zh
                score = max(sim, 0.6)
            elif winner == "mt":
                action = "replace_with_mt"
                final_zh = recommendation or mt_zh
                score = max(1 - sim, 0.7)
            else:
                action = "human_review"
                final_zh = old_zh
                score = 0.5

        result = ValidationResult(
            term_id=term_id,
            source_lang=source_lang,
            source_term=source_term,
            old_zh=old_zh,
            mt_zh=mt_zh,
            final_zh=final_zh,
            action=action,
            reason=reason,
            ambiguity_tags=ambiguity_tags,
            score=round(score, 4),
            category=category,
            search_freq=freq,
        )

        result_rows.append(result.__dict__)
        if action == "human_review":
            review_rows.append(result.__dict__)

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    save_csv(out_dir / "validated_terms.csv", result_rows, CANONICAL_OUTPUT_FIELDS)
    save_csv(out_dir / "review_queue.csv", review_rows, CANONICAL_OUTPUT_FIELDS)

    metrics = {
        "total_terms": len(rows),
        "mt_calls": mt_calls,
        "llm_calls": llm_calls,
        "human_review_count": len(review_rows),
        "elapsed_seconds": round(time.time() - start, 3),
        "hunyuan_mode": args.hunyuan_mode,
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
