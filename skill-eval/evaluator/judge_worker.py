#!/usr/bin/env python3
"""
Judge Worker — runs in an isolated subprocess for token-isolated LLM judging.

Problem it solves
----------------
When running inside a parent agent (Cursor/CI), calling LLM APIs directly
consumes the parent agent's token budget. This worker runs as a subprocess
with its own API calls, completely isolating token accounting.

Features
--------
1. Batch judging: evaluate multiple criteria in a single API call
2. Token isolation: all API calls happen in subprocess, not parent
3. Fallback: if batch fails, falls back to individual judgments

Wire protocol (stdio, one request → one response)
-------------------------------------------------
stdin  : single JSON line (request)
stdout : single JSON line (response)

Request schema
--------------
{
  "output":        str,           # The output to evaluate
  "criteria":      list[dict],    # List of criteria with assertion_id, criterion, score_levels
  "prompt":        str,           # Optional: user prompt context
  "expected":      str,           # Optional: expected behavior context
  "settings": {
    "anthropic_api_key": str,
    "openai_api_key": str,
    "openai_base_url": str,
    "judge_model": str,
  }
}

Response schema
---------------
{
  "results": list[dict],    # [{"assertion_id": ..., "score": 0-1, "level": "...", "reasoning": "...", "eval_method": "batch_judge|fallback"}]
  "total_tokens": int,
  "error": str | null
}
"""
from __future__ import annotations

import json
import os
import re
import sys


# Default score levels
DEFAULT_SCORE_LEVELS = {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}


def _build_prompt(output: str, criteria: list[dict], prompt: str = "", expected: str = "") -> str:
    """Build a batch evaluation prompt for multiple criteria."""
    import datetime  # pylint: disable=import-outside-toplevel
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    context = f"今天是 {today_str}。\n"
    if prompt:
        context += f"用户Prompt: {prompt[:200]}\n"
    if expected:
        context += f"期望行为: {expected[:200]}\n"

    criteria_list = []
    for i, c in enumerate(criteria, 1):
        criterion = c.get("criterion", "")
        levels = c.get("score_levels", DEFAULT_SCORE_LEVELS)
        levels_text = "\n".join(f"  - {label} → {value}" for label, value in levels.items())
        criteria_list.append(f"""### 评估维度 {i}: {c.get("assertion_id", f"criterion_{i}")}

评分标准: {criterion}

评分档位:
{levels_text}""")

    prompt_text = f"""你是一个严格的 AI Skill 评测 Judge。请根据以下多个评分标准对输出进行批量评估。

{context}
---
待评测输出:
{output[:1500]}

---
评估维度:
{chr(10).join(criteria_list)}

---
请直接返回 JSON 数组格式（不加任何多余文字），每个元素对应一个评估维度：
[{{"assertion_id": "维度ID", "level": "完全满足|部分满足|不满足", "reasoning": "评分理由（30字内）"}}]"""

    return prompt_text


async def _call_judge(prompt: str, settings: dict) -> tuple[str, int]:
    """Call LLM judge API and return (response_text, token_count)."""
    import anthropic
    import openai

    api_key = settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = settings.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
    openai_base = settings.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL", "")
    model = settings.get("judge_model", "claude-opus-4-5")

    # Try Anthropic first
    if api_key:
        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model=model,
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip() if resp.content else ""
            tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
            return text, tokens
        except (anthropic.APIError, anthropic.APIConnectionError) as e:
            print(f"[judge_worker] anthropic failed: {e}", file=sys.stderr)

    # Fallback to OpenAI-compatible
    if openai_key:
        try:
            client = openai.AsyncOpenAI(
                api_key=openai_key,
                base_url=openai_base or "https://api.openai.com/v1",
            )
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            msg = resp.choices[0].message if resp.choices else None
            text = (msg.content or "").strip() if msg else ""
            tokens = (resp.usage.prompt_tokens or 0) + (resp.usage.completion_tokens or 0) if resp.usage else 0
            return text, tokens
        except (openai.APIError, openai.APIConnectionError) as e:
            print(f"[judge_worker] openai failed: {e}", file=sys.stderr)

    raise RuntimeError("No LLM provider available")


def _extract_batch_results(text: str, criteria: list[dict]) -> list[dict]:
    """Extract batch results from judge response."""
    del criteria
    # Try to extract JSON array
    try:
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        # Try direct parse
        try:
            arr = json.loads(cleaned)
            if isinstance(arr, list):
                return arr
        except json.JSONDecodeError:
            pass

        # Try to find array in text
        match = re.search(r'\[[\s\S]*\]', cleaned)
        if match:
            arr = json.loads(match.group())
            if isinstance(arr, list):
                return arr
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"[judge_worker] parse failed: {e}", file=sys.stderr)

    # Fallback: return empty, will use fallback judgment
    return []


def _fallback_judge(output: str, criterion: dict) -> dict:
    """Simple fallback judgment when LLM fails."""
    criterion_text = criterion.get("criterion", "")
    score_levels = criterion.get("score_levels", DEFAULT_SCORE_LEVELS)

    # For Chinese text, try to match meaningful substrings (2-6 chars)
    output_lower = output.lower()
    criterion_lower = criterion_text.lower()

    # Extract potential keywords: 2-6 character substrings that might be meaningful
    # Skip common stop words and single characters
    stop_words = {'是否', '包含', '应该', '必须', '需要', '能够', '可以', '具有', '满足', '符合', '达到', '输出', '内容', '是否包含', '是否满足', '是否具有', '一个', '这个', '那个', '什么', '如何', '怎样', '为什么'}

    # Try all substrings of length 2-6
    keywords = []
    for length in range(2, 7):
        for i in range(len(criterion_lower) - length + 1):
            substr = criterion_lower[i:i+length]
            if substr not in stop_words and substr not in keywords:
                keywords.append(substr)

    # Score: count how many keywords appear in output
    matches = sum(1 for kw in keywords if kw in output_lower)

    # Normalize by number of potential keywords
    match_ratio = matches / max(len(keywords), 1)

    if match_ratio >= 0.3:
        level = "完全满足"
        score = score_levels.get("完全满足", 1.0)
        reasoning = f"关键词匹配: {matches}/{len(keywords)}"
    elif match_ratio >= 0.1:
        level = "部分满足"
        score = score_levels.get("部分满足", 0.5)
        reasoning = f"部分匹配: {matches}/{len(keywords)}"
    else:
        level = "不满足"
        score = score_levels.get("不满足", 0.0)
        reasoning = "未匹配到相关关键词"

    return {
        "assertion_id": criterion.get("assertion_id", ""),
        "level": level,
        "score": score,
        "reasoning": reasoning,
        "eval_method": "fallback"
    }


async def _judge_batch(output: str, criteria: list[dict], settings: dict,
                       prompt: str = "", expected: str = "") -> tuple[list[dict], int]:
    """Judge multiple criteria in batch."""
    if not criteria:
        return [], 0

    # Build batch prompt
    batch_prompt = _build_prompt(output, criteria, prompt, expected)

    try:
        response_text, tokens = await _call_judge(batch_prompt, settings)
        results = _extract_batch_results(response_text, criteria)

        if len(results) >= len(criteria) * 0.5:  # At least 50% success rate
            # Map results to criteria
            mapped = []
            for c in criteria:
                cid = c.get("assertion_id", "")
                # Find matching result
                matched = next((r for r in results if r.get("assertion_id") == cid), None)
                if matched:
                    score_levels = c.get("score_levels", DEFAULT_SCORE_LEVELS)
                    level = matched.get("level", "不满足")
                    mapped.append({
                        "assertion_id": cid,
                        "level": level,
                        "score": score_levels.get(level, 0.0),
                        "reasoning": matched.get("reasoning", ""),
                        "eval_method": "batch_judge"
                    })
                else:
                    mapped.append(_fallback_judge(output, c))
            return mapped, tokens
        else:
            raise RuntimeError("Batch parse failed - insufficient results returned")
    except (json.JSONDecodeError, ValueError, TypeError, RuntimeError) as e:
        print(f"[judge_worker] batch failed: {e}, using fallback", file=sys.stderr)
        # Fallback to individual judgments
        results = []
        total_tokens = 0
        for c in criteria:
            result = _fallback_judge(output, c)
            results.append(result)
        return results, total_tokens


async def main() -> None:
    """Main entry point."""

    try:
        raw = sys.stdin.readline()
        if not raw.strip():
            _respond({"results": [], "total_tokens": 0, "error": "empty request"})
            return

        req = json.loads(raw)
        output = req.get("output", "")
        criteria = req.get("criteria", [])
        prompt = req.get("prompt", "")
        expected = req.get("expected", "")
        settings = req.get("settings", {})

        # Add environment variables to settings if not provided
        for key in ["anthropic_api_key", "openai_api_key", "openai_base_url"]:
            if key not in settings:
                settings[key] = os.environ.get(key.upper(), "")

        results, tokens = await _judge_batch(output, criteria, settings, prompt, expected)

        _respond({
            "results": results,
            "total_tokens": tokens,
            "error": None
        })

    except (json.JSONDecodeError, ValueError, TypeError, RuntimeError, OSError) as e:
        print(f"[judge_worker] error: {e}", file=sys.stderr)
        _respond({"results": [], "total_tokens": 0, "error": str(e)})


def _respond(data: dict):
    """Write response to stdout."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())