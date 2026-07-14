"""LLM-as-Judge: supports Anthropic API and OpenAI-compatible endpoints."""

from __future__ import annotations

import json
import os
import structlog

from evaluator.config import get_settings
from evaluator.models.exceptions import JudgeError

logger = structlog.get_logger()


class LLMJudge:
    """Wrapper around LLM APIs for scoring skill outputs.

    Provider priority:
      1. Anthropic (ANTHROPIC_API_KEY)
      2. OpenAI-compatible (OPENAI_API_KEY + optional OPENAI_BASE_URL)

    All layer modules must use this class — never call SDKs directly from layers.
    Token usage is returned alongside scores so callers can aggregate consumption.

    The model can be overridden at construction time via the ``model`` argument,
    which takes precedence over the ``JUDGE_MODEL`` env-var / settings value.
    This lets callers (CLI, pipeline, tests) specify a per-run judge model without
    mutating global settings or env vars.
    """

    def __init__(
        self,
        model: str | None = None,
        eval_model: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self._model_override = model
        self._eval_model_override = eval_model
        self._anthropic_client = None
        self._openai_client = None

    @property
    def _judge_model(self) -> str:
        """Effective judge model: explicit override > settings > default."""
        return self._model_override or self.settings.judge_model

    @property
    def eval_model(self) -> str:
        """Effective eval-execution model: explicit override > settings > default."""
        return self._eval_model_override or self.settings.eval_model

    # ── utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Extract the first valid JSON object from a response that may contain extra text.

        Handles thinking models (e.g. GLM-5) that mix reasoning prose with the
        final JSON answer, wrap it in markdown fences, or return partial responses.
        Strategy: try several increasingly lenient extraction approaches.
        """
        import re  # pylint: disable=import-outside-toplevel
        if not text:
            raise ValueError("empty response")

        # 1. Strip markdown code fences
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        # 2. Direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 3. Find ALL {...} blocks (greedy), try them from last to first
        #    (final answer is usually last)
        brace_blocks = list(re.finditer(r"\{[^{}]+\}", cleaned, re.DOTALL))
        for block in reversed(brace_blocks):
            try:
                return json.loads(block.group())
            except json.JSONDecodeError:
                continue

        # 4. Try to reconstruct truncated JSON: find "level" value and build dict
        level_match = re.search(r'"level"\s*:\s*"([^"]+)"', text)
        reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]+)"', text)
        if level_match:
            return {
                "level": level_match.group(1),
                "reasoning": reasoning_match.group(1) if reasoning_match else "（截断）",
            }

        raise ValueError(f"No valid JSON found in response: {text[:100]!r}")

    # ── provider detection ────────────────────────────────────────────────────

    def _active_provider(self) -> str:
        """Return 'anthropic', 'openai', or 'none'."""
        if self.settings.anthropic_api_key:
            return "anthropic"
        api_key = self.settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            return "openai"
        return "none"

    def _get_client(self):
        """Return Anthropic async client (legacy helper used by layer4)."""
        if self._anthropic_client is None:
            try:
                import anthropic  # pylint: disable=import-outside-toplevel
                self._anthropic_client = anthropic.AsyncAnthropic(
                    api_key=self.settings.anthropic_api_key
                )
            except ImportError as exc:
                raise JudgeError("anthropic package not installed") from exc
        return self._anthropic_client

    def _get_openai_client(self):
        """Return AsyncOpenAI client configured from settings / env."""
        if self._openai_client is None:
            try:
                import openai  # pylint: disable=import-outside-toplevel
            except ImportError as exc:
                raise JudgeError("openai package not installed; run: pip install openai") from exc
            api_key = self.settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
            base_url = self.settings.openai_base_url or os.environ.get("OPENAI_BASE_URL", "") or None
            self._openai_client = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=60.0,  # 60s per request; prevents infinite hang on slow endpoints
            )
        return self._openai_client

    # ── unified call helper ───────────────────────────────────────────────────

    async def _chat(self, prompt: str, max_tokens: int = 200) -> tuple[str, int]:
        """Send a single-turn chat and return (response_text, token_count).

        For thinking/reasoning models (e.g. GLM-5, o1) that put the final answer
        in `content` and reasoning in `reasoning_content`, we boost max_tokens to
        ensure there is budget for the actual answer after the reasoning chain,
        and fall back to reasoning_content when content is empty.
        """
        provider = self._active_provider()
        # Thinking models need extra token budget for reasoning + final answer
        effective_max = max(max_tokens, 2048)
        if provider == "anthropic":
            client = self._get_client()
            resp = await client.messages.create(
                model=self._judge_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip() if resp.content else ""
            tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
            return text, tokens
        if provider == "openai":
            client = self._get_openai_client()
            model = self._judge_model
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=effective_max,
                messages=[{"role": "user", "content": prompt}],
            )
            msg = resp.choices[0].message if resp.choices else None
            text = ""
            if msg:
                # content is the final answer; reasoning_content is the thinking chain.
                # We always prefer content; only fall back to reasoning_content when
                # content is completely empty (thinking model ran out of tokens for answer).
                # Note: reasoning_content is the raw thinking chain and may contain the
                # answer embedded within it — _extract_json handles that case.
                content = (msg.content or "").strip()
                reasoning_content = (getattr(msg, "reasoning_content", None) or "").strip()
                text = content if content else reasoning_content
            tokens = (resp.usage.prompt_tokens or 0) + (resp.usage.completion_tokens or 0) if resp.usage else 0
            return text, tokens
        raise JudgeError("No LLM provider configured (set ANTHROPIC_API_KEY or OPENAI_API_KEY)")

    # ── public API ────────────────────────────────────────────────────────────

    async def score_correctness(
        self,
        output: str,
        criterion: str,
        scoring_guidance: str,
        score_levels: dict[str, float] | None = None,
    ) -> tuple[float, str, int]:
        """Ask the LLM judge to score an output against one criterion.

        Returns:
            (score_0_to_1, reasoning_text, token_count)

        Raises:
            JudgeError: On API failure.
        """
        if score_levels is None:
            score_levels = {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}

        levels_text = "\n".join(f"- {label} → {value}" for label, value in score_levels.items())
        prompt = f"""你是一个专业的 AI Skill 评测 Judge。请根据以下评分标准对输出进行评分。

评分标准：{criterion}

评分指引：{scoring_guidance}

评分档位：
{levels_text}

待评测输出：
{output[:2000]}

请直接返回 JSON，格式如下（不加任何多余文字）：
{{"level": "完全满足|部分满足|不满足", "reasoning": "简短的评分理由（50字内）"}}"""

        try:
            # Thinking models (e.g. GLM-5) use most tokens for reasoning; final JSON needs budget
            raw, tokens = await self._chat(prompt, max_tokens=3000)
            data = self._extract_json(raw)
            level = data.get("level", "不满足")
            score = score_levels.get(level, 0.0)
            reasoning = data.get("reasoning", "")
            return score, reasoning, tokens
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("judge.score_failed", error=str(exc))
            return 0.5, f"Judge error: {exc}", 0

    async def generate_scoring_guidance(self, criterion: str, context: str) -> tuple[str, int]:
        """Generate dynamic scoring guidance for a test case criterion.

        Returns:
            (guidance_text, token_count)
        """
        prompt = f"""为以下评分标准生成具体的评分指引（60字内，指导 LLM Judge 如何判断）：

评分标准：{criterion}
用例背景：{context[:200]}

直接返回评分指引文本，不需要其他格式。"""

        try:
            text, tokens = await self._chat(prompt, max_tokens=100)
            return text, tokens
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("judge.guidance_failed", error=str(exc))
            return f"评估输出是否满足：{criterion}", 0
