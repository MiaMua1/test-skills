"""Layer 4: Dynamic evaluation — run test cases with and without skill (v5)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from evaluator.config import get_score_profiles
from evaluator.models.exceptions import BlockedError, ScoreBindingError
from evaluator.models.skill import SkillInfo

logger = structlog.get_logger()

# Profiles that prefer programmatic field-checking over LLM judge for correctness
PROGRAMMATIC_FIRST_PROFILES = {"deterministic", "workflow"}


class Layer4Dynamic:
    """Layer 4: Execute all test cases, save per-case snapshots, compute scores.

    For generator / no_code profiles: skip without_skill runs (delta=0).
    Blocked if ALL P0 cases fail robustness checks.
    """

    layer_number = 4
    layer_name = "layer4_dynamic"

    def __init__(self, skill_info: SkillInfo, storage_base: Path,
                 judge: object, skip_baseline: bool = False,
                 with_baseline: bool = False) -> None:
        self.skill_info = skill_info
        self.skill_path = skill_info.skill_path
        self.profile = skill_info.eval_profile.value
        self.weights = get_score_profiles(with_baseline)[self.profile]
        self.storage_base = storage_base
        self.judge = judge
        self.skip_baseline = skip_baseline
        self.log = logger.bind(layer=self.layer_name, skill=skill_info.metadata.name)
        self._cli_path_cache: str | None | bool = False  # False = not yet probed
        self._cli_env_cache: dict | None = None  # env vars that make the CLI work

        # Circuit breaker: disable CLI after consecutive failures
        self._cli_consecutive_failures: int = 0
        self._cli_disabled: bool = False

        # API auth failure circuit breaker: abort after 3 consecutive API errors
        self._api_auth_failure_count: int = 0
        self._api_auth_blocked: bool = False

        # v6: 进度日志文件 - 父进程可读取感知进度
        self._progress_file: Path | None = None
        self._current_eval_id: str | None = None

    async def run(self, eval_id: str, *, skip_tc_ids: set[str] | None = None) -> dict:
        """Execute all test cases for the given eval_id.

        Returns:
            Dict with robust_score, correct_score, delta_score, per-case results.

        Raises:
            ScoreBindingError: eval_id mismatch.
            BlockedError: All P0 cases failed robustness.
        """
        evals_dir = self.storage_base / "evals" / self.skill_info.metadata.name / eval_id
        evals_path = evals_dir / "evals.json"
        criteria_path = evals_dir / "scoring_criteria.json"

        t_start = time.monotonic()

        # v6: 初始化进度日志
        self._init_progress_file(eval_id)

        if not evals_path.exists() or not criteria_path.exists():
            return {"status": "skipped", "reason": "No evals.json / scoring_criteria.json found"}

        evals_data = json.loads(evals_path.read_text(encoding="utf-8"))
        criteria_data = json.loads(criteria_path.read_text(encoding="utf-8"))

        # Validate eval_id binding
        if evals_data.get("eval_id") != eval_id or criteria_data.get("eval_id") != eval_id:
            raise ScoreBindingError("eval_id mismatch between evals.json and scoring_criteria.json", eval_id)

        # Validate weight sum: base weights (excluding delta) must equal 100
        snapshot = criteria_data.get("profile_weight_snapshot", {})
        base_weight_sum = sum(v for k, v in snapshot.items() if k != "delta_max")
        if abs(base_weight_sum - 100.0) > 0.01:
            raise ScoreBindingError(f"profile_weight_snapshot base sum={base_weight_sum} ≠ 100", eval_id)

        test_cases = evals_data.get("test_cases", [])
        # Preserve original order for sorting after resume merge
        all_tc_order = {tc["id"]: i for i, tc in enumerate(test_cases)}
        criteria_by_tc = {c["tc_id"]: c for c in criteria_data.get("criteria_by_tc", [])}
        # skip_baseline=True means user provided both evals+criteria: no delta comparison needed
        needs_delta = self.weights.delta_max > 0 and not self.skip_baseline

        results_dir_with = self.storage_base / "results" / self.skill_info.metadata.name / eval_id / "with_skill"
        results_dir_without = self.storage_base / "results" / self.skill_info.metadata.name / eval_id / "without_skill"
        results_dir_with.mkdir(parents=True, exist_ok=True)
        results_dir_without.mkdir(parents=True, exist_ok=True)

        per_case: list[dict] = []

        # Resume support: skip already-completed test cases
        if skip_tc_ids:
            original_count = len(test_cases)
            # Load existing results for skipped cases
            for tc in test_cases:
                if tc["id"] in skip_tc_ids:
                    existing = self._load_existing_result(eval_id, tc["id"])
                    if existing:
                        per_case.append(existing)
            test_cases = [tc for tc in test_cases if tc["id"] not in skip_tc_ids]
            self.log.info("layer4.resume_skip", skipped=original_count - len(test_cases),
                          remaining=len(test_cases))
            if not test_cases:
                self.log.info("layer4.resume_all_done", message="All test cases already completed")
                # Jump to aggregation with loaded results

        # Dynamic concurrency:
        # - Claude CLI (direct): each TC takes 20-60s → run 3 in parallel
        # - Tool-calling LLM API (agent loop): multiple API calls per TC → serialize to avoid 429
        # - Plain LLM API: keep at 2 to reduce server-side queuing
        cli_available = bool(self._find_claude_cli())
        openai_compat = self._detect_openai_compat()
        n_tc = len(test_cases)
        if cli_available:
            # Independent subprocess per TC — safe to run 3 in parallel
            max_concurrent = min(n_tc, 3)
        elif openai_compat and not self._has_api_key():
            # Tool-calling path: each TC spawns 2-8 API calls; run sequentially to avoid 429
            max_concurrent = 1
        else:
            max_concurrent = min(n_tc, 2)
        semaphore = asyncio.Semaphore(max(max_concurrent, 1))

        tc_total = len(test_cases)
        tc_done = 0

        # v6: 更新进度 - 开始执行
        self._update_progress("running", total=tc_total, completed=0,
                            current=None, status=f"开始执行 {tc_total} 个测试用例")

        _API_AUTH_FAILURE_THRESHOLD = 3

        async def _run_tc(tc: dict) -> dict:
            nonlocal tc_done
            tc_id = tc["id"]
            criteria = criteria_by_tc.get(tc_id, tc_id)

            # Skip remaining TCs if API auth failure threshold reached
            if self._api_auth_blocked:
                tc_done += 1
                self.log.warning("layer4.tc_skipped_api_auth", tc_id=tc_id,
                                 reason="API authentication failure threshold reached")
                return {"tc_id": tc_id, "priority": tc["priority"],
                        "with": {"status": "skipped_api_auth", "scores": {"robust_raw": 0, "correct_raw": 0},
                                 "output": {"raw_response": "Skipped: API authentication failed"},
                                 "execution": {"failure_tags": ["api_auth_blocked"], "status": "skipped"}},
                        "without": None}

            # v6: 更新进度 - 开始执行当前用例
            self._update_progress("running", total=tc_total, completed=tc_done,
                                current=tc_id, status=f"执行中: {tc_id}")

            async with semaphore:
                with_result = await self._run_single(
                    tc, "with_skill", eval_id, criteria, results_dir_with
                )

            # Check for API auth failure and update counter
            with_status = with_result.get("status", "")
            if with_status == "api_auth_error":
                self._api_auth_failure_count += 1
                self.log.warning(
                    "layer4.api_auth_failure_detected",
                    tc_id=tc_id,
                    consecutive_failures=self._api_auth_failure_count,
                    threshold=_API_AUTH_FAILURE_THRESHOLD,
                )
                if self._api_auth_failure_count >= _API_AUTH_FAILURE_THRESHOLD:
                    self._api_auth_blocked = True
                    self._update_progress(
                        "blocked", total=tc_total, completed=tc_done,
                        current=tc_id,
                        status=f"API 认证失败已达 {_API_AUTH_FAILURE_THRESHOLD} 次，终止评测。请检查 API Key 配置后重新评测。",
                    )
            else:
                # Reset counter on successful invocation
                self._api_auth_failure_count = 0

            entry: dict = {"tc_id": tc_id, "priority": tc["priority"],
                           "with": with_result, "without": None}
            if needs_delta and tc.get("baseline_prompt") and not self._api_auth_blocked:
                async with semaphore:
                    without_result = await self._run_single(
                        tc, "without_skill", eval_id, criteria, results_dir_without
                    )
                entry["without"] = without_result
            tc_done += 1

            # v6: 更新进度 - 当前用例完成
            status_detail = with_result.get("status", "unknown")
            if with_result.get("scores", {}).get("correct_raw", 0) == 0:
                status_detail += " (正确性得分=0)"
            self._update_progress("running", total=tc_total, completed=tc_done,
                                current=tc_id, status=f"完成: {tc_id} [{status_detail}]")

            self.log.info(
                "layer4.tc_done",
                tc_id=tc_id,
                progress=f"{tc_done}/{tc_total}",
                correct=round(with_result.get("scores", {}).get("correct_raw", 0), 3),
            )
            return entry

        results = await asyncio.gather(*[_run_tc(tc) for tc in test_cases])
        # Merge resumed (skipped) results with newly executed results
        per_case.extend(results)
        # Preserve original test case order (using all_tc_order from before resume filtering)
        per_case = sorted(per_case, key=lambda r: all_tc_order.get(r["tc_id"], 999))
        execution_breakdown = self._build_execution_breakdown(per_case)

        # Aggregate scores
        robust_scores = [c["with"].get("scores", {}).get("robust_raw", 0) for c in per_case]
        correct_with = [c["with"].get("scores", {}).get("correct_raw", 0) for c in per_case]
        correct_without = [c["without"].get("scores", {}).get("correct_raw", 0) if c["without"] else 0 for c in per_case]

        avg_robust = sum(robust_scores) / len(robust_scores) if robust_scores else 0.0
        avg_correct_with = sum(correct_with) / len(correct_with) if correct_with else 0.0
        avg_correct_without = sum(correct_without) / len(correct_without) if correct_without else 0.0

        robust_score = round(avg_robust * self.weights.robust_max, 2)
        correct_score = round(avg_correct_with * self.weights.correct_max, 2)

        if needs_delta:
            delta_raw = avg_correct_with - avg_correct_without
            delta_normalized = max(0.0, delta_raw + 0.5)
            delta_score = round(delta_normalized * self.weights.delta_max, 2)
        else:
            delta_raw = 0.0
            delta_normalized = 0.0
            delta_score = 0.0

        # Blocking check: API authentication failure
        if self._api_auth_blocked:
            raise BlockedError(
                layer=4, score=0,
                reason=(
                    f"API 认证连续失败 {self._api_auth_failure_count} 次，评测终止。"
                    " 请检查 API Key 配置是否正确，修复后重新评测。"
                ),
            )

        # Blocking check: all P0 robustness failures
        p0_cases = [c for c in per_case if c["priority"] == "P0"]
        if p0_cases:
            p0_robust_all_fail = all(
                not any(r.get("passed") for r in c["with"].get("robustness_results", []))
                for c in p0_cases
            )
            if p0_robust_all_fail:
                raise BlockedError(layer=4, score=0,
                                   reason="All P0 test case robustness checks failed")

        duration = round(time.monotonic() - t_start, 3)

        # v5 §5.3: record to performance knowledge base
        perf_entry = {
            "eval_id": eval_id,
            "evaluated_at": datetime.now(tz=timezone.utc).isoformat(),
            "avg_duration_ms": round(
                sum(c["with"].get("duration_seconds", 0) for c in per_case) / max(len(per_case), 1) * 1000, 1
            ),
            "avg_token_usage": round(
                sum(c["with"].get("token_count", 0) for c in per_case) / max(len(per_case), 1), 0
            ),
            "test_case_count": len(per_case),
            "total_score": round(robust_score + correct_score + delta_score, 2),
        }
        perf_baseline = self._record_perf_baseline(perf_entry)

        active_llm_mode = self._llm_mode()
        result = {
            "status": "completed",
            "layer": 4,
            "duration_s": duration,
            "robust_score": robust_score,
            "robust_max": self.weights.robust_max,
            "correct_score": correct_score,
            "correct_max": self.weights.correct_max,
            "delta_score": delta_score,
            "delta_max": self.weights.delta_max,
            "delta_raw": round(delta_raw, 4),
            "delta_normalized": round(delta_normalized, 4),
            "with_correct": round(avg_correct_with, 4),
            "without_correct": round(avg_correct_without, 4),
            "total_score": round(robust_score + correct_score + delta_score, 2),
            "llm_mode": active_llm_mode,  # "anthropic" | "openai" | "none"
            "performance_baseline": perf_baseline,
            "execution_breakdown": execution_breakdown,
            "per_case": per_case,
        }
        self.log.info("layer4.complete",
                      robust=robust_score, correct=correct_score, delta=delta_score,
                      duration_s=duration)

        # v6: 更新最终进度日志
        self._update_progress("completed", total=tc_total, completed=tc_total,
                             current=None, status="评测完成")

        return result

    # ── 进度日志功能 (v6) ────────────────────────────────────────────────

    def _load_existing_result(self, eval_id: str, tc_id: str) -> dict | None:
        """Load an existing test case result from storage for resume."""
        with_file = (self.storage_base / "results" / self.skill_info.metadata.name
                     / eval_id / "with_skill" / f"{tc_id}.json")
        if not with_file.exists():
            return None
        try:
            with_data = json.loads(with_file.read_text(encoding="utf-8"))
            without_data = None
            without_file = (self.storage_base / "results" / self.skill_info.metadata.name
                            / eval_id / "without_skill" / f"{tc_id}.json")
            if without_file.exists():
                without_data = json.loads(without_file.read_text(encoding="utf-8"))
            return {
                "tc_id": tc_id,
                "priority": with_data.get("input", {}).get("context", {}).get("priority", "P1"),
                "with": with_data,
                "without": without_data,
            }
        except (json.JSONDecodeError, OSError):
            return None

    def _init_progress_file(self, eval_id: str) -> None:
        """初始化进度日志文件。"""
        self._current_eval_id = eval_id
        progress_dir = self.storage_base / "progress" / self.skill_info.metadata.name
        progress_dir.mkdir(parents=True, exist_ok=True)
        self._progress_file = progress_dir / f"{eval_id}.json"

        # 写入初始状态
        self._update_progress("initializing", total=0, completed=0,
                             current=None, status="评测初始化中")

    def _update_progress(self, phase: str, total: int = 0, completed: int = 0,
                        current: str | None = None, status: str = "",
                        error: str | None = None) -> None:
        """更新进度日志文件，供父进程轮询。"""
        if not self._progress_file:
            return

        progress_data = {
            "eval_id": self._current_eval_id,
            "skill_name": self.skill_info.metadata.name,
            "phase": phase,  # initializing, running, completed, error
            "total": total,
            "completed": completed,
            "current": current,
            "status": status,
            "error": error,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        try:
            self._progress_file.write_text(
                json.dumps(progress_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:  # pylint: disable=broad-except
            pass  # 日志写入失败不阻塞主流程

    def get_progress(self) -> dict | None:
        """获取当前进度，供父进程调用。"""
        if not self._progress_file or not self._progress_file.exists():
            return None
        try:
            return json.loads(self._progress_file.read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-except
            return None

    def _record_perf_baseline(self, entry: dict) -> dict:
        """Append to eval-knowledge-base JSONL and return comparison info (v5 §5.3)."""
        kb_dir = self.storage_base / "eval-knowledge-base" / "evaluations"
        kb_dir.mkdir(parents=True, exist_ok=True)
        kb_path = kb_dir / f"{self.skill_info.metadata.name}.jsonl"

        history: list[dict] = []
        if kb_path.exists():
            for line in kb_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        history.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Append new entry
        with kb_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        if not history:
            return {"status": "首次评测", "message": "首次评测，无历史对比", "warning": False}

        prev = history[-1]
        prev_dur = prev.get("avg_duration_ms", 0)
        cur_dur = entry["avg_duration_ms"]

        if prev_dur == 0:
            return {"status": "首次有效对比", "message": "历史数据缺少耗时信息", "warning": False}

        change_pct = (cur_dur - prev_dur) / prev_dur * 100
        if change_pct > 50:
            msg = f"⚠️ 性能显著退化（+{change_pct:.0f}%）"
            score_also_improved = entry["total_score"] > prev.get("total_score", 0)
            if score_also_improved:
                msg += " — 功能优化但代价是性能，建议关注"
            return {"status": "性能退化", "message": msg, "warning": True,
                    "change_pct": round(change_pct, 1)}
        if change_pct < -20:
            return {"status": "性能提升", "message": f"性能提升 {abs(change_pct):.0f}%",
                    "warning": False, "change_pct": round(change_pct, 1)}
        return {"status": "性能持平", "message": f"性能变化 {change_pct:+.0f}%（正常范围）",
                "warning": False, "change_pct": round(change_pct, 1)}

    async def _run_single(self, tc: dict, run_mode: str, eval_id: str,
                          criteria: dict, output_dir: Path) -> dict:
        """Run a single test case and save its snapshot."""
        tc_id = tc["id"]
        prompt = tc["prompt"] if run_mode == "with_skill" else tc.get("baseline_prompt", tc["prompt"])
        started = datetime.now(tz=timezone.utc)
        case_start = time.monotonic()

        # Generous per-TC timeout: data-analysis skills (table-analyst etc.) need time
        # to load Excel files, run pandas, and generate responses (≤600s = 10 min).
        TC_TIMEOUT_S = 600
        RETRY_BACKOFF_S = 5.0
        MAX_RETRIES = 1

        output, invoke_duration, status = await self._invoke_with_retry(
            prompt, run_mode == "with_skill", tc_id,
            timeout_s=TC_TIMEOUT_S, backoff_s=RETRY_BACKOFF_S, max_retries=MAX_RETRIES,
        )

        # Check response completeness (detect waiting-for-user / incomplete answers)
        final_ans = output.get("final_answer", "") or output.get("raw_response", "")
        response_complete = self._is_response_complete(final_ans)
        if not response_complete:
            self.log.warning(
                "layer4.response_incomplete",
                tc_id=tc_id, run_mode=run_mode,
                hint="model asked for input or stopped mid-task — incomplete answer",
            )
            output["incomplete_response"] = True

        execution = self._build_execution_record(run_mode=run_mode, status=status, output=output)
        output["execution"] = execution

        # Evaluate robustness
        robustness_results = self._eval_robustness(tc, output, criteria.get("robustness_scoring", []))

        # Skip expensive LLM judge for hard failures (timeout/error/stub) only.
        # Soft failures like incomplete_response still have usable output worth evaluating.
        hard_failure_tags = {"tc_timeout", "stub_output", "invoke_failed", "api_error", "tooling_failed"}
        actual_failure_tags = set(execution.get("failure_tags", []))
        is_total_failure = bool(actual_failure_tags & hard_failure_tags) or status == "error"
        if is_total_failure:
            self.log.info("layer4.skip_judge", tc_id=tc_id, reason="total_failure",
                          failure_tags=execution.get("failure_tags", []))
            correctness_results = [{
                "assertion_id": rule.get("assertion_id", ""),
                "criterion": rule.get("criterion", ""),
                "score": 0.0,
                "level": "不满足",
                "reasoning": "跳过评分：用例执行完全失败",
                "tokens": 0,
                "judge_duration_s": 0,
                "eval_method": "skipped_total_failure",
                "needs_human_review": False,
            } for rule in criteria.get("correctness_scoring", [])]
            judge_tokens = 0
        else:
            correctness_results, judge_tokens = await self._eval_correctness(
                tc, output, criteria.get("correctness_scoring", [])
            )

        robust_raw = self._agg_robustness(robustness_results, criteria.get("robustness_scoring", []))
        correct_raw = self._agg_correctness(correctness_results, criteria.get("correctness_scoring", []))

        invoke_tokens = output.get("token_count", 0) or 0
        total_tokens = invoke_tokens + judge_tokens
        total_duration = round(time.monotonic() - case_start, 3)

        snapshot = {
            "tc_id": tc_id,
            "eval_id": eval_id,
            "skill_name": self.skill_info.metadata.name,
            "run_mode": run_mode,
            "executed_at": started.isoformat(),
            "duration_seconds": total_duration,
            "invoke_duration_s": round(invoke_duration, 3),
            "token_count": total_tokens,
            "invoke_tokens": invoke_tokens,
            "judge_tokens": judge_tokens,
            "status": status,
            "input": {
                "prompt": prompt,
                "skill_used": self.skill_info.metadata.name if run_mode == "with_skill" else None,
                "skill_version": self.skill_info.metadata.version,
                "context": tc.get("context", {}),
            },
            "output": output,
            "execution": execution,
            "robustness_results": robustness_results,
            "correctness_results": correctness_results,
            "scores": {"robust_raw": robust_raw, "correct_raw": correct_raw},
        }

        out_file = output_dir / f"{tc_id}.json"
        out_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return snapshot

    # ── skill invocation ─────────────────────────────────────────────────────

    def _has_api_key(self) -> bool:
        """True if Anthropic API key is configured (primary LLM provider)."""
        return bool(self.judge.settings.anthropic_api_key)

    def _detect_openai_compat(self) -> dict | None:
        """Detect OpenAI-compatible API key/base-URL from settings or environment variables.

        Priority: EvalSettings (from .env) → os.environ fallback.
        Supports OPENAI_API_KEY (OpenAI / 三方 proxy) and any service that
        exposes OPENAI_BASE_URL pointing at an OpenAI-compatible endpoint.
        Returns a dict with 'api_key', 'base_url', and 'model' if found, else None.
        """
        settings = self.judge.settings
        api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        base_url = settings.openai_base_url or os.environ.get("OPENAI_BASE_URL", "")
        if api_key:
            return {
                "api_key": api_key,
                "base_url": base_url or "https://api.openai.com/v1",
                "model": self.judge.eval_model,
            }
        return None

    def _llm_mode(self) -> str:
        """Return the available LLM invocation mode: 'anthropic', 'openai', or 'none'."""
        if self._has_api_key():
            return "anthropic"
        if self._detect_openai_compat():
            return "openai"
        return "none"

    def _find_entry_point(self) -> dict | None:
        """Locate an executable entry point in the skill directory."""
        py_candidates = ["skill_entry.py", "main.py", "app.py", "run.py"]
        for name in py_candidates:
            p = self.skill_path / name
            if p.exists():
                return {"cmd": [sys.executable, str(p)], "type": f"python:{name}"}

        js_candidates = ["index.js", "src/index.js", "main.js"]
        for name in js_candidates:
            p = self.skill_path / name
            if p.exists():
                return {"cmd": ["node", str(p)], "type": f"node:{name}"}

        scripts_dir = self.skill_path / "scripts"
        if scripts_dir.exists():
            for sh in sorted(scripts_dir.glob("*.sh")):
                return {"cmd": ["bash", str(sh)], "type": f"bash:{sh.name}"}

        return None

    @staticmethod
    def _is_api_auth_error(raw_response: str) -> bool:
        """Detect API authentication or authorization errors in response text.

        Matches common error patterns from Claude CLI, Anthropic API, and
        OpenAI-compatible APIs that indicate invalid/expired credentials.
        """
        if not raw_response:
            return False
        lower = raw_response.lower()
        auth_error_patterns = [
            "failed to authenticate",
            "authentication_error",
            "invalid api key",
            "invalid x-api-key",
            "api key not found",
            "unauthorized",
            "401",
            "permission denied",
            "access denied",
            "invalid_api_key",
            "incorrect api key",
            "expired api key",
            "api key expired",
        ]
        api_error_indicators = ["api error", "api_error", "apierror"]
        has_auth_pattern = any(pattern in lower for pattern in auth_error_patterns)
        has_api_indicator = any(indicator in lower for indicator in api_error_indicators)
        # Match if both auth pattern + api indicator, or just strong auth patterns
        if has_auth_pattern and has_api_indicator:
            return True
        # Strong standalone patterns that always indicate auth failure
        strong_patterns = [
            "failed to authenticate",
            "authentication_error",
            "invalid api key",
            "invalid x-api-key",
            "invalid_api_key",
        ]
        return any(pattern in lower for pattern in strong_patterns)

    async def _invoke_with_retry(
        self, prompt: str, use_skill: bool, tc_id: str, *,
        timeout_s: float = 600, backoff_s: float = 5.0, max_retries: int = 1,
    ) -> tuple[dict, float, str]:
        """Invoke skill with timeout and retry on failure.

        Returns (output, invoke_duration, status).
        Retries only on timeout or exception — not on success.
        """
        last_output: dict = {}
        last_duration: float = 0.0
        last_status: str = "failed"

        for attempt in range(max_retries + 1):
            try:
                output = await asyncio.wait_for(
                    self._invoke_skill(prompt, use_skill),
                    timeout=timeout_s,
                )
                invoke_duration = output.get("invoke_duration_s", 0.0)

                # Detect API auth errors hidden in successful responses
                raw_resp = output.get("raw_response", "") or ""
                if self._is_api_auth_error(raw_resp):
                    output["api_error"] = raw_resp[:500]
                    output["is_stub"] = True
                    return output, invoke_duration, "api_auth_error"

                return output, invoke_duration, "success"
            except asyncio.TimeoutError:
                last_output = {
                    "raw_response": f"TimeoutError: execution exceeded {timeout_s}s",
                    "tool_calls": [], "final_answer": "", "token_count": 0,
                    "invoke_duration_s": float(timeout_s), "is_stub": True,
                }
                last_duration = float(timeout_s)
                last_status = "timeout"
            except Exception as exc:  # pylint: disable=broad-except
                last_output = {
                    "raw_response": str(exc), "tool_calls": [], "final_answer": "",
                    "token_count": 0, "invoke_duration_s": 0.0, "is_stub": True,
                }
                last_duration = 0.0
                last_status = "failed"

            if attempt < max_retries:
                self.log.warning(
                    "layer4.invoke_retry",
                    tc_id=tc_id, attempt=attempt + 1, status=last_status,
                    backoff_s=backoff_s,
                )
                await asyncio.sleep(backoff_s)

        return last_output, last_duration, last_status

    async def _invoke_skill(self, prompt: str, use_skill: bool) -> dict:
        """Invoke the skill with the given prompt.

        Priority:
        1. `claude` CLI available → _invoke_via_claude_cli (最准确).
        2. Anthropic API key configured → _invoke_via_llm (provider=anthropic).
        3. OpenAI-compatible API → _invoke_via_llm_with_tools (agent loop + Python execution).
        4. No LLM AND use_skill=True → _invoke_locally (subprocess entry point).
        5. No LLM AND use_skill=False → static placeholder.
        """
        attempts: list[dict] = []

        def _record_attempt(stage: str, result: dict) -> None:
            attempts.append({
                "stage": stage,
                "provider": result.get("provider", "unknown"),
                "is_stub": bool(result.get("is_stub", False)),
                "simulation_note": result.get("simulation_note", ""),
                "api_error": result.get("api_error", ""),
            })

        def _finalize(result: dict) -> dict:
            result["execution_attempts"] = attempts
            return result

        if not self._cli_disabled and self._find_claude_cli():
            cli_result = await self._invoke_via_claude_cli(prompt, use_skill)
            _record_attempt("claude_cli", cli_result)
            if cli_result.get("is_stub"):
                self._cli_consecutive_failures += 1
                if self._cli_consecutive_failures >= 2:
                    self._cli_disabled = True
                    self.log.warning(
                        "layer4.cli_circuit_breaker_open",
                        consecutive_failures=self._cli_consecutive_failures,
                        message="CLI disabled, falling back to LLM API",
                    )
                # Fall through to LLM API instead of returning stub
            else:
                self._cli_consecutive_failures = 0
                return _finalize(cli_result)

        mode = self._llm_mode()
        if mode == "anthropic":
            llm_result = await self._invoke_via_llm(prompt, use_skill, provider="anthropic")
            _record_attempt("anthropic_api", llm_result)
            return _finalize(llm_result)
        if mode == "openai":
            llm_result = await self._invoke_via_llm_with_tools(prompt, use_skill, provider="openai")
            _record_attempt("openai_api", llm_result)
            return _finalize(llm_result)
        if use_skill:
            local_result = await self._invoke_locally(prompt)
            _record_attempt("local_entry", local_result)
            return _finalize(local_result)
        baseline_result = {
            "raw_response": (
                "[降级模式 baseline] 未检测到 claude CLI 或任何 LLM API Key。"
                "基线对比不可用，delta 分值置 0。\n"
                f"Prompt: {prompt[:200]}"
            ),
            "tool_calls": [],
            "final_answer": "[降级模式 baseline — 无 LLM 可用]",
            "token_count": 0,
            "invoke_duration_s": 0.0,
            "is_stub": True,
            "degraded": True,
            "simulation_note": "⚠️ 无 claude CLI / LLM — baseline 跳过",
        }
        _record_attempt("baseline_degraded", baseline_result)
        return _finalize(baseline_result)

    # ── claude CLI invocation (primary path) ─────────────────────────────────

    @staticmethod
    def _in_claude_code_session() -> bool:
        """Return True when running inside a Claude Code / Cursor agent execution context.

        Detection signals (any one is sufficient):
        - CLAUDECODE=1       : set by claude CLI for every child process it spawns.
                               Launching a nested claude here is explicitly forbidden.
        - CURSOR_AGENT=1     : set by Cursor IDE when the AI agent runs shell commands.
        - CURSOR_EXTENSION_HOST_ROLE=agent-exec : Cursor agent-exec extension host role.

        In all these cases we are already inside an AI coding environment that can execute
        code directly, so spawning a nested claude CLI subprocess is both unnecessary and
        blocked. We fall through to the LLM API path instead.
        """
        env = os.environ
        return (
            env.get("CLAUDECODE") == "1"
            or env.get("CURSOR_AGENT") == "1"
            or env.get("CURSOR_EXTENSION_HOST_ROLE") == "agent-exec"
        )

    def _find_claude_cli(self) -> str | None:
        """Return the path to the `claude` binary if it is usable (authenticated + responsive).

        Returns None when:
        - Running inside a Claude Code / Cursor session (CLAUDECODE=1) — nested launch forbidden.
        - No claude binary found on PATH or known locations.
        - Liveness probe times out or fails for all candidates.

        When a usable binary is found the validated env dict is cached in self._cli_env_cache
        for reuse in _invoke_via_claude_cli (CLAUDECODE is always stripped from that env).
        """
        # Return cached result (avoids repeated liveness probes per TC)
        if self._cli_path_cache is not False:
            return self._cli_path_cache  # type: ignore[return-value]

        # Candidate binaries to try in order
        candidates: list[str] = []
        std_binary = shutil.which("claude")
        if std_binary:
            candidates.append(std_binary)
        codefuse_binary = Path.home() / ".codefuse" / "fuse" / "engine" / "hooks" / "mac-arm64" / "claude"
        if codefuse_binary.exists() and str(codefuse_binary) not in candidates:
            candidates.append(str(codefuse_binary))

        if not candidates:
            self._cli_path_cache = None
            return None

        # Build env: strip all agent-session markers so claude CLI starts cleanly
        # even when the evaluator itself is running inside a Cursor / Claude Code session.
        _AGENT_ENV_KEYS = {
            "CLAUDECODE", "CURSOR_AGENT", "CURSOR_EXTENSION_HOST_ROLE",
            "CLAUDE_CONFIG_DIR",  # Prevent inheriting codefuse/engine/cc config with antchat/auto model
        }
        cli_env = {k: v for k, v in os.environ.items() if k not in _AGENT_ENV_KEYS}
        try:
            settings_path = Path.home() / ".claude" / "settings.json"
            if settings_path.exists():
                _settings = json.loads(settings_path.read_text(encoding="utf-8"))
                cli_env.update(_settings.get("env", {}))
        except Exception:  # pylint: disable=broad-except
            pass

        # Override base URL to local proxy when available — avoids slow remote auth during startup
        LOCAL_PROXY = "http://127.0.0.1:9382"
        try:
            import urllib.request  # pylint: disable=import-outside-toplevel
            urllib.request.urlopen(LOCAL_PROXY, timeout=1)  # type: ignore[attr-defined]
            cli_env["ANTHROPIC_BASE_URL"] = LOCAL_PROXY
        except Exception:  # pylint: disable=broad-except
            pass  # Proxy not running — keep original base URL

        for binary in candidates:
            try:
                probe = subprocess.run(  # pylint: disable=subprocess-run-check
                    [binary, "--version"],
                    capture_output=True, text=True, timeout=5, env=cli_env,
                )
                if probe.returncode != 0 and not probe.stdout.strip():
                    self.log.info("layer4.claude_cli_skip",
                                  binary=binary, reason=f"liveness probe failed (exit {probe.returncode})")
                    continue
                self.log.info("layer4.claude_cli_found",
                              binary=binary, version=probe.stdout.strip()[:40])
                self._cli_path_cache = binary
                self._cli_env_cache = cli_env
                return binary
            except subprocess.TimeoutExpired:
                # --version hangs inside agent sessions (CLAUDECODE env stripped but
                # the binary still waits for a TTY in some versions). Treat timeout
                # as "binary exists and is likely usable" — same as test_claude_cli.py.
                self.log.info("layer4.claude_cli_found",
                              binary=binary, version="version_probe_timeout_assume_ok")
                self._cli_path_cache = binary
                self._cli_env_cache = cli_env
                return binary
            except Exception:  # pylint: disable=broad-except
                self.log.info("layer4.claude_cli_found",
                              binary=binary, version="probe_error_assume_ok")
                self._cli_path_cache = binary
                self._cli_env_cache = cli_env
                return binary

        self.log.info("layer4.claude_cli_skip", reason="all candidate binaries failed liveness probe")
        self._cli_path_cache = None
        return None

    def _find_project_root(self) -> Path:
        """Find a suitable project root that has (or can have) a .claude/ directory.

        Walks upward from cwd looking for an existing .claude/ dir (mirrors how
        claude -p discovers its project root). Falls back to cwd.
        """
        cwd = Path.cwd()
        for candidate in [cwd, *cwd.parents]:
            if (candidate / ".claude").is_dir():
                return candidate
        return cwd

    def _clear_commands_dir(self, commands_dir: Path) -> None:
        """Remove all existing skill command files from .claude/commands/.

        Only removes .md files to avoid accidentally deleting other config files.
        """
        if not commands_dir.exists():
            return
        for md_file in commands_dir.glob("*.md"):
            try:
                md_file.unlink()
            except OSError:
                pass

    # File extensions treated as readable text when collecting skill content
    _TEXT_EXTENSIONS = {
        ".md", ".txt", ".py", ".js", ".ts", ".jsx", ".tsx",
        ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".conf",
        ".sh", ".bash", ".zsh", ".fish",
        ".html", ".css", ".xml", ".csv", ".sql",
        ".r", ".R", ".jl", ".rb", ".go", ".rs", ".java", ".kt", ".scala",
        ".c", ".cpp", ".h", ".hpp",
        ".env", ".env.example", ".gitignore", ".dockerignore",
        "Dockerfile", "Makefile",
    }

    # Directories to skip when recursively collecting skill files
    _SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}

    def _collect_skill_files(self) -> dict[str, str]:
        """Collect ALL text files from the skill directory recursively.

        Returns a mapping of relative_path → file_content.
        SKILL.md is always placed first (if present) for priority ordering.
        Binary files (images, Excel, compiled, etc.) are skipped.
        """
        collected: dict[str, str] = {}
        skill_path = self.skill_path

        # Priority 1: SKILL.md — always first
        skill_md = skill_path / "SKILL.md"
        if skill_md.exists():
            try:
                collected["SKILL.md"] = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

        # Priority 2: Recursively collect all other text files
        for file_path in sorted(skill_path.rglob("*")):
            if not file_path.is_file():
                continue

            # Skip files inside excluded directories
            if any(part in self._SKIP_DIRS for part in file_path.parts):
                continue

            rel = str(file_path.relative_to(skill_path))
            if rel in collected:
                continue

            # Check if the file is a known text type
            suffix = file_path.suffix.lower()
            name = file_path.name
            is_text = (
                suffix in self._TEXT_EXTENSIONS
                or name in self._TEXT_EXTENSIONS  # e.g. "Dockerfile", "Makefile"
                or name.startswith(".")  # dotfiles like .eslintrc
            )
            if not is_text:
                continue

            try:
                collected[rel] = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

        return collected

    def _build_skill_system_prompt(self) -> str:
        """Build a lightweight system prompt that points Claude to the skill files.

        The full skill content is already available in the sandbox filesystem
        (copied by _create_isolated_sandbox), so we only inject a brief
        instruction here instead of embedding all file contents — which would
        exceed OS ARG_MAX for large skills.
        """
        skill_name = self.skill_info.metadata.name
        skill_md_exists = (self.skill_path / "SKILL.md").exists()
        if not skill_md_exists and not any(self.skill_path.rglob("*")):
            return ""

        lines = [
            f"你已安装了名为「{skill_name}」的技能（Skill）。",
            "技能的完整文件已放置在当前工作目录的 skill/ 子目录下。",
        ]
        if skill_md_exists:
            lines.append("请先阅读 skill/SKILL.md 了解技能的使用说明和规范，然后严格按照该技能的要求来回答用户的问题。")
        else:
            lines.append("请阅读 skill/ 目录下的文件了解技能内容，然后严格按照技能的要求来回答用户的问题。")

        return "\n".join(lines)

    def _create_isolated_sandbox(self, use_skill: bool) -> tuple[Path, "tempfile.TemporaryDirectory[str]"]:
        """Create a temporary directory as an isolated Claude sandbox.

        The sandbox has its own .claude/commands/ directory so the evaluation
        does not interfere with the user's real Claude environment.

        with_skill=True  → copies the ENTIRE skill directory into the sandbox,
                           preserving the full directory structure (all .md, .py,
                           .json, scripts/, docs/, etc.)
        with_skill=False → .claude/commands/ stays empty (clean baseline)

        Returns a (path, TemporaryDirectory) tuple. The caller must call
        ``tmpdir_obj.cleanup()`` when done (or use it as a context manager).
        """
        import tempfile  # pylint: disable=import-outside-toplevel
        tmpdir_obj = tempfile.TemporaryDirectory(prefix="skill-eval-sandbox-")
        sandbox = Path(tmpdir_obj.name)
        commands_dir = sandbox / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)

        if use_skill:
            # Copy the entire skill directory into the sandbox
            skill_dest = sandbox / "skill"
            shutil.copytree(
                str(self.skill_path),
                str(skill_dest),
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".git", "node_modules",
                ),
            )
            copied_count = sum(1 for _ in skill_dest.rglob("*") if _.is_file())
            self.log.info(
                "layer4.sandbox_skill_installed",
                sandbox=str(sandbox),
                skill_dir=str(skill_dest),
                files_copied=copied_count,
            )

            # Also install SKILL.md as a Claude command for discoverability
            skill_md_path = skill_dest / "SKILL.md"
            if skill_md_path.exists():
                target = commands_dir / f"{self.skill_info.metadata.name}.md"
                target.write_text(
                    skill_md_path.read_text(encoding="utf-8"), encoding="utf-8"
                )

        return sandbox, tmpdir_obj

    async def _invoke_via_claude_cli(self, prompt: str, use_skill: bool) -> dict:
        """Execute prompt via `claude -p` in an isolated sandbox environment.

        Creates a fresh temporary directory as the working directory so that:
        - The user's real .claude/commands/ is never touched
        - No pre-existing skills interfere with the evaluation
        - --disable-slash-commands disables all built-in slash commands

        with_skill=True  → sandbox contains a full copy of the skill directory,
                           SKILL.md is also installed as a Claude command,
                           and ALL skill files are injected via --append-system-prompt
        with_skill=False → sandbox .claude/commands/ is empty, no skill injection (clean baseline)

        The sandbox is cleaned up after each invocation.
        """
        t0 = time.monotonic()
        claude_bin = self._find_claude_cli()
        if not claude_bin:
            return {
                "raw_response": "[claude CLI 未找到]",
                "tool_calls": [], "final_answer": "",
                "token_count": 0, "invoke_duration_s": 0.0, "is_stub": True,
                "simulation_note": "claude CLI not found",
            }

        sandbox_dir: Path | None = None
        sandbox_tmpdir: "tempfile.TemporaryDirectory[str] | None" = None
        try:
            # Create isolated sandbox — never touches the user's real .claude/ environment
            sandbox_dir, sandbox_tmpdir = self._create_isolated_sandbox(use_skill)
            self.log.info("layer4.sandbox_created",
                          sandbox=str(sandbox_dir), use_skill=use_skill)

            # Use the env that was validated during liveness probe (includes local proxy + tokens).
            # _cli_env_cache is already stripped of agent-session markers by _find_claude_cli.
            # If cache is missing, build a clean env on the spot.
            _AGENT_ENV_KEYS = {
                "CLAUDECODE", "CURSOR_AGENT", "CURSOR_EXTENSION_HOST_ROLE",
                "CLAUDE_CONFIG_DIR",
            }
            env = (dict(self._cli_env_cache) if self._cli_env_cache
                   else {k: v for k, v in os.environ.items() if k not in _AGENT_ENV_KEYS})

            # Inject current date so Claude can reason about relative time references
            today_str = datetime.now().strftime("%Y年%m月%d日（%A）")
            date_system_prompt = (
                f"当前日期是{today_str}。请务必基于此日期回答所有与时间、月份、季度、年份相关的问题。"
            )

            # Build the full system prompt: date context + (optional) skill content
            if use_skill:
                skill_system_prompt = self._build_skill_system_prompt()
                if not skill_system_prompt:
                    return {
                        "raw_response": "[skill 目录下未找到任何可读文件，无法加载 skill 内容]",
                        "tool_calls": [], "final_answer": "",
                        "token_count": 0, "invoke_duration_s": 0.0, "is_stub": True,
                        "simulation_note": "no readable skill files found",
                    }
                full_system_prompt = date_system_prompt + "\n\n" + skill_system_prompt
                self.log.info("layer4.skill_injected_into_system_prompt",
                              skill=self.skill_info.metadata.name,
                              system_prompt_chars=len(full_system_prompt))
            else:
                full_system_prompt = date_system_prompt

            cmd = [
                claude_bin, "-p", prompt,
                "--output-format", "json",
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
                "--append-system-prompt", full_system_prompt,
            ]

            # CLI takes 20-60s for real data analysis; give generous timeout
            # Data-analysis skills (table-analyst etc.) may need up to 600s.
            CLI_TIMEOUT_S = 600
            loop = asyncio.get_event_loop()
            try:
                proc_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: subprocess.run(  # pylint: disable=subprocess-run-check
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=CLI_TIMEOUT_S,
                            cwd=str(sandbox_dir),
                            env=env,
                        ),
                    ),
                    timeout=CLI_TIMEOUT_S + 20,
                )
            except asyncio.TimeoutError:
                return {
                    "raw_response": f"[claude CLI 调用超时 {CLI_TIMEOUT_S}s]",
                    "tool_calls": [], "final_answer": "",
                    "token_count": 0, "invoke_duration_s": float(CLI_TIMEOUT_S), "is_stub": True,
                    "simulation_note": "claude CLI timeout",
                }

            stdout = proc_result.stdout.strip()
            stderr = proc_result.stderr.strip()
            duration = round(time.monotonic() - t0, 3)

            # Parse JSON output {"result": "...", "cost_usd": ..., "usage": {...}}
            answer = stdout
            token_count = 0
            try:
                parsed = json.loads(stdout)
                answer = parsed.get("result", stdout)
                usage = parsed.get("usage", {})
                token_count = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("output_tokens") or 0)
                )
            except (json.JSONDecodeError, TypeError):
                pass  # stdout is plain text — use as-is

            if not answer and proc_result.returncode != 0:
                answer = f"[claude CLI 失败 exit={proc_result.returncode}] {stderr[:300]}"

            sim_note = (
                "claude_cli:with_skill (isolated sandbox + skill injected into system prompt)"
                if use_skill
                else "claude_cli:without_skill (isolated sandbox, clean baseline)"
            )

            return {
                "raw_response": answer,
                "tool_calls": [],
                "final_answer": answer,
                "token_count": token_count,
                "invoke_duration_s": duration,
                "is_stub": False,
                "exit_code": proc_result.returncode,
                "simulation_note": sim_note,
                "provider": "claude_cli",
            }
        except Exception as exc:  # pylint: disable=broad-except
            self.log.warning("layer4.claude_cli_failed", error=str(exc))
            return {
                "raw_response": f"[claude CLI 异常] {exc}",
                "tool_calls": [], "final_answer": "",
                "token_count": 0,
                "invoke_duration_s": round(time.monotonic() - t0, 3),
                "is_stub": True,
                "simulation_note": f"claude CLI error: {str(exc)[:80]}",
            }
        finally:
            # Clean up sandbox — never leave temp dirs behind
            if sandbox_tmpdir is not None:
                try:
                    sandbox_tmpdir.cleanup()
                    self.log.info("layer4.sandbox_cleaned", sandbox=str(sandbox_dir))
                except OSError:
                    pass



    def _load_skill_md(self) -> str:
        """Read the full SKILL.md content from the skill directory."""
        skill_md_path = self.skill_path / "SKILL.md"
        if skill_md_path.exists():
            return skill_md_path.read_text(encoding="utf-8")
        return ""

    def _build_with_skill_system_ctx(self) -> str:
        """Build system context that actually loads the full SKILL.md instructions.

        This is the key difference from pure LLM simulation: the model receives
        the complete SKILL.md as its operating instructions, just as it would in
        a real Cursor session where the skill is loaded into context.
        """
        skill_md = self._load_skill_md()
        skill_name = self.skill_info.metadata.name
        if skill_md:
            return (
                f"你正在使用以下 Skill 来处理用户请求。"
                f"请严格遵循 Skill 中的所有指令、格式要求和工作流程。\n\n"
                f"=== SKILL: {skill_name} ===\n"
                f"{skill_md}\n"
                f"=== END SKILL ===\n\n"
                f"请按照上述 Skill 的完整指令处理接下来的用户请求。"
            )
        # Fallback: description only
        desc = self.skill_info.metadata.description or f"AI Skill: {skill_name}"
        return (
            f"你是具备「{skill_name}」技能的 AI 助手。\n技能描述：{desc[:500]}\n\n"
            f"请以该技能的身份完整处理用户请求，输出尽量符合技能的预期行为和格式。"
        )

    def _build_without_skill_system_ctx(self) -> str:
        """Build a neutral baseline system context for without_skill (对照组) runs.

        Simulates what a clean claude / LLM session looks like WITHOUT the skill
        loaded — same model capability, no skill-specific instructions, no artificial
        restriction of capabilities.  This gives a fair apples-to-apples comparison:
        the only variable is whether SKILL.md is injected or not.

        Intentionally does NOT say "you have no tools" — that would cripple the
        baseline and produce a misleadingly large delta score.
        """
        return (
            "你是 Claude，Anthropic 训练的 AI 助手。\n"
            "请直接、尽力地完成用户的请求。\n"
            "（对照组 baseline：本次会话未加载任何额外技能或指令注入，"
            "请以通用 AI 助手的默认能力处理任务。）"
        )

    async def _invoke_via_llm(self, prompt: str, use_skill: bool,
                              provider: str = "anthropic") -> dict:
        """Call the configured LLM API to simulate skill execution.

        with_skill=True  → injects the FULL SKILL.md as system context so the model
                           actually follows the skill's instructions (not just a
                           truncated description).
        with_skill=False → neutral claude-like baseline, no skill context injected
                           (same model, same capabilities — only the skill is absent).

        Supports 'anthropic' (primary) and 'openai' (OpenAI-compatible, fallback).
        """
        t0 = time.monotonic()

        system_ctx = (
            self._build_with_skill_system_ctx() if use_skill
            else self._build_without_skill_system_ctx()
        )

        sim_note = (
            f"with_skill：{provider} + full SKILL.md loaded" if use_skill
            else f"without_skill：{provider} baseline（clean claude，无 skill 注入）"
        )

        try:
            if provider == "anthropic":
                client = self.judge._get_client()  # pylint: disable=protected-access
                resp = await client.messages.create(
                    model=self.judge.eval_model,
                    max_tokens=4096,
                    system=system_ctx,
                    messages=[{"role": "user", "content": prompt}],
                )
                # Only keep text blocks; skip thinking/reasoning blocks
                answer = ""
                for block in (resp.content or []):
                    block_type = getattr(block, "type", None)
                    if block_type == "text":
                        answer = block.text
                        break
                if not answer and resp.content:
                    # Fallback: first block with a text attribute that is not thinking
                    for block in resp.content:
                        if getattr(block, "type", "") != "thinking" and hasattr(block, "text"):
                            answer = block.text
                            break
                tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
            else:
                # OpenAI-compatible path (openai package optional)
                answer, tokens = await self._invoke_via_openai(prompt, system_ctx)

            return {
                "raw_response": answer,
                "tool_calls": [],
                "final_answer": answer,
                "token_count": tokens,
                "invoke_duration_s": round(time.monotonic() - t0, 3),
                "is_stub": False,
                "provider": provider,
                "simulation_note": sim_note,
            }
        except Exception as exc:  # pylint: disable=broad-except
            self.log.warning("layer4.llm_invoke_failed", provider=provider, error=str(exc))
            return {
                "raw_response": f"[LLM 调用失败({provider})] {exc}",
                "tool_calls": [],
                "final_answer": "",
                "token_count": 0,
                "invoke_duration_s": round(time.monotonic() - t0, 3),
                "is_stub": True,
                "api_error": str(exc)[:200],
                "simulation_note": f"{provider} 调用失败: {str(exc)[:80]}",
            }

    def _execute_python_code(self, code: str) -> str:
        """Execute Python code in a subprocess and return stdout + stderr.

        Security constraints:
        - 30-second wall-clock timeout (prevents infinite loops / long data loads)
        - Subprocess isolation (code runs in a child process, not this interpreter)

        Returns a string with execution output, or an error message.
        """
        try:
            result = subprocess.run(  # pylint: disable=subprocess-run-check
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout.strip()
            if result.stderr.strip():
                # Append stderr only when there is something to report
                output = (output + "\n[stderr]: " + result.stderr.strip()[:800]).strip()
            return output or "[no stdout output]"
        except subprocess.TimeoutExpired:
            return "[execute_python] 执行超时（30s），可能是数据量过大或死循环"
        except Exception as exc:  # pylint: disable=broad-except
            return f"[execute_python] 执行失败: {exc}"

    async def _invoke_via_llm_with_tools(self, prompt: str, use_skill: bool,
                                          provider: str = "openai") -> dict:
        """OpenAI-compatible agent loop with local Python execution tool.

        The model can call `execute_python(code)` to actually run pandas / numpy
        analysis on the data files referenced in the prompt, getting real numbers
        instead of just generating code descriptions.

        Loop terminates when:
        - Model returns a final text answer (finish_reason != "tool_calls")
        - MAX_TOOL_ITERATIONS is reached (returns partial answer + warning)
        - Any API error occurs (falls back to no-tools path)
        """
        MAX_TOOL_ITERATIONS = 8
        t0 = time.monotonic()

        compat = self._detect_openai_compat()
        if not compat:
            # No OpenAI compat — fall back to plain LLM call
            return await self._invoke_via_llm(prompt, use_skill, provider="anthropic")

        today_str = datetime.now().strftime("%Y年%m月%d日（%A）")
        date_ctx = f"\n\n【当前日期】{today_str}。请基于此日期回答所有与时间、月份、季度、年份相关的问题。"
        system_ctx = (
            self._build_with_skill_system_ctx() if use_skill
            else self._build_without_skill_system_ctx()
        ) + date_ctx
        sim_note = (
            f"with_skill：{provider} + tools + full SKILL.md" if use_skill
            else f"without_skill：{provider} + tools baseline"
        )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "execute_python",
                    "description": (
                        "在本地 Python 环境中执行代码并返回标准输出。"
                        "可用库：pandas、numpy、matplotlib、openpyxl、seaborn。"
                        "文件路径直接使用用户提供的绝对路径。"
                        "执行超时限制 30 秒。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "要执行的 Python 代码",
                            }
                        },
                        "required": ["code"],
                    },
                },
            }
        ]

        messages: list[dict] = [{"role": "user", "content": prompt}]
        all_tool_calls: list[dict] = []
        total_tokens = 0

        try:
            import openai as _openai  # pylint: disable=import-outside-toplevel
            client = _openai.AsyncOpenAI(
                api_key=compat["api_key"],
                base_url=compat["base_url"] or None,
            )
            model = compat.get("model") or self.judge.eval_model or "gpt-4o-mini"

            async def _call_with_retry(msgs: list[dict], max_retries: int = 3) -> object:
                """Call the LLM with exponential backoff on 429 rate-limit errors."""
                for attempt in range(max_retries + 1):
                    try:
                        return await client.chat.completions.create(
                            model=model,
                            max_tokens=4096,
                            messages=[{"role": "system", "content": system_ctx}] + msgs,
                            tools=tools,
                            tool_choice="auto",
                        )
                    except _openai.RateLimitError:
                        if attempt == max_retries:
                            raise
                        wait = 2 ** attempt * 5  # 5s, 10s, 20s
                        self.log.warning("layer4.tools_rate_limit",
                                         attempt=attempt + 1, wait_s=wait)
                        await asyncio.sleep(wait)
                return None  # unreachable

            for iteration in range(MAX_TOOL_ITERATIONS):
                resp = await _call_with_retry(messages)

                choice = resp.choices[0] if resp.choices else None
                if resp.usage:
                    total_tokens += (resp.usage.prompt_tokens or 0) + (resp.usage.completion_tokens or 0)

                if not choice:
                    break

                assistant_msg: dict = {"role": "assistant"}
                if choice.message.content:
                    assistant_msg["content"] = choice.message.content
                if choice.message.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in choice.message.tool_calls
                    ]
                messages.append(assistant_msg)

                if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                    # Model finished — return its final answer
                    final_answer = choice.message.content or ""
                    duration = round(time.monotonic() - t0, 3)
                    return {
                        "raw_response": final_answer,
                        "tool_calls": all_tool_calls,
                        "final_answer": final_answer,
                        "token_count": total_tokens,
                        "invoke_duration_s": duration,
                        "is_stub": False,
                        "provider": provider,
                        "simulation_note": sim_note,
                        "tool_iterations": iteration + 1,
                    }

                # Execute all tool calls in this turn
                tool_result_msgs: list[dict] = []
                for tc in choice.message.tool_calls:
                    if tc.function.name == "execute_python":
                        try:
                            args = json.loads(tc.function.arguments)
                            code = args.get("code", "")
                        except (json.JSONDecodeError, KeyError):
                            code = tc.function.arguments
                        exec_output = self._execute_python_code(code)
                        all_tool_calls.append({
                            "tool": "execute_python",
                            "code_snippet": code[:200],
                            "output_snippet": exec_output[:300],
                        })
                        self.log.info(
                            "layer4.tool_executed",
                            tool="execute_python",
                            iteration=iteration,
                            output_len=len(exec_output),
                        )
                        tool_result_msgs.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": exec_output[:3000],  # cap to avoid context overflow
                        })
                    else:
                        tool_result_msgs.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"[未知工具: {tc.function.name}]",
                        })
                messages.extend(tool_result_msgs)

            # Max iterations reached — return whatever the last content was
            last_content = next(
                (m.get("content", "") for m in reversed(messages) if m["role"] == "assistant"),
                "[已达最大工具调用轮次，未获得最终答案]",
            )
            return {
                "raw_response": last_content,
                "tool_calls": all_tool_calls,
                "final_answer": last_content,
                "token_count": total_tokens,
                "invoke_duration_s": round(time.monotonic() - t0, 3),
                "is_stub": False,
                "provider": provider,
                "simulation_note": sim_note + f" [max_iterations={MAX_TOOL_ITERATIONS}]",
                "tool_iterations": MAX_TOOL_ITERATIONS,
                "warning": "max tool iterations reached",
            }

        except Exception as exc:  # pylint: disable=broad-except
            self.log.warning("layer4.tools_invoke_failed", error=str(exc))
            # Fall back to plain LLM without tools
            self.log.info("layer4.tools_fallback", reason=str(exc)[:80])
            fallback_result = await self._invoke_via_llm(prompt, use_skill, provider="openai")
            fallback_result["tooling_failed"] = True
            fallback_result["tooling_error"] = str(exc)[:200]
            return fallback_result

    async def _invoke_via_openai(self, prompt: str, system_ctx: str) -> tuple[str, int]:
        """Call an OpenAI-compatible endpoint using OPENAI_API_KEY + OPENAI_BASE_URL."""
        compat = self._detect_openai_compat()
        if not compat:
            raise RuntimeError("No OPENAI_API_KEY found")
        try:
            import openai  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed; run: pip install openai"
            ) from exc

        client = openai.AsyncOpenAI(
            api_key=compat["api_key"],
            base_url=compat["base_url"] or None,
        )
        model = compat.get("model") or self.judge.eval_model or "gpt-4o-mini"
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system_ctx},
                {"role": "user", "content": prompt},
            ],
        )
        answer = ""
        if resp.choices:
            msg = resp.choices[0].message
            # Only use content (final answer), never fall back to reasoning_content
            answer = (msg.content or "").strip()
        tokens = (resp.usage.prompt_tokens or 0) + (resp.usage.completion_tokens or 0) if resp.usage else 0
        return answer, tokens

    async def _invoke_locally(self, prompt: str) -> dict:
        """Run the skill's entry point locally, passing the prompt via stdin."""
        t0 = time.monotonic()
        entry = self._find_entry_point()

        if entry is None:
            # Attach SKILL.md content so rule-based correctness eval can score against documentation
            skill_md_path = self.skill_path / "SKILL.md"
            skill_content = skill_md_path.read_text(encoding="utf-8") if skill_md_path.exists() else ""
            return {
                "raw_response": "[本地执行] 未找到可执行入口（skill_entry.py / index.js / scripts/*.sh）",
                "tool_calls": [],
                "final_answer": "[本地执行失败：无入口]",
                "token_count": 0,
                "invoke_duration_s": 0.0,
                "is_stub": True,
                "simulation_note": "local: no entry point found",
                "skill_content": skill_content,
            }

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(  # pylint: disable=subprocess-run-check
                    entry["cmd"],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(self.skill_path),
                    env={**__import__("os").environ},
                ),
            )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            answer = stdout or (f"[stderr] {stderr[:500]}" if stderr else "[no output]")
            duration = round(time.monotonic() - t0, 3)
            return {
                "raw_response": f"{answer}\n[exit {result.returncode}]" + (f"\n[stderr] {stderr[:200]}" if stderr and result.returncode != 0 else ""),
                "tool_calls": [],
                "final_answer": answer,
                "token_count": 0,
                "invoke_duration_s": duration,
                "is_stub": False,
                "exit_code": result.returncode,
                "simulation_note": f"local: {entry['type']} (exit {result.returncode})",
            }
        except subprocess.TimeoutExpired:
            return {
                "raw_response": "[本地执行超时 30s]",
                "tool_calls": [],
                "final_answer": "[timeout]",
                "token_count": 0,
                "invoke_duration_s": 30.0,
                "is_stub": True,
                "simulation_note": "local: timeout",
            }
        except Exception as exc:  # pylint: disable=broad-except
            self.log.warning("layer4.local_invoke_failed", error=str(exc))
            return {
                "raw_response": f"[本地执行失败] {exc}",
                "tool_calls": [],
                "final_answer": "",
                "token_count": 0,
                "invoke_duration_s": round(time.monotonic() - t0, 3),
                "is_stub": True,
                "simulation_note": f"local: error {str(exc)[:60]}",
            }

    def _is_response_complete(self, answer: str) -> bool:
        """Detect if the response is a complete final answer vs. asking for user input.

        Returns False if the model stopped mid-task waiting for the user to provide
        execution results or answer a question (common in simulation mode when the
        model can't actually run scripts).

        Key distinction: if the response already contains substantive analysis content
        (tables, data, conclusions) but ends with a polite follow-up question like
        "需要我进一步分析吗？", that is NOT incomplete — the main task is done.
        Only flag as incomplete when the *entire* response is asking for input without
        having delivered any analysis result.
        """
        if not answer:
            return False

        # Signals that the response contains substantive analysis content.
        # If any of these are present, the model has already delivered results —
        # a trailing follow-up question does not make it "incomplete".
        substantive_indicators = [
            r"\|.*\|.*\|",                    # markdown table row
            r"#{1,3}\s+.{4,}",               # markdown heading with content
            r"\d[\d,]*\.\d+",                # decimal number (data value)
            r"(总计|合计|总零售|总销售|平均|占比|排名|Top\s*\d)",  # aggregation keywords
            r"(分析结果|关键发现|数据概览|统计摘要|趋势)",         # analysis section headers
        ]
        has_substantive_content = any(
            re.search(p, answer) for p in substantive_indicators
        )

        # Patterns that indicate the model is truly waiting for user input
        # (not just appending a polite follow-up at the end of a complete answer)
        incomplete_signals = [
            r"请(先|首先|帮我)?(执行|运行|提供|告诉我|确认|分享)",
            r"请问(你|您)(是否|能否|可以)",
            r"awaiting.?execution.?results",
            r"一旦(我|获得)(执行|结果|数据)",
            r"(你|您)可以(直接|先)(让我|提供|告诉)",
            r"能否提供.{0,30}(结果|数据|信息)",
            r"(执行|运行)后.*?我(将|会|再)",
            r"请(稍等|先运行|先执行)",
        ]

        if has_substantive_content:
            # Response already has real analysis — trailing questions are just politeness
            return True

        for pattern in incomplete_signals:
            if re.search(pattern, answer, re.IGNORECASE):
                return False
        return True

    def _eval_robustness(self, _tc: dict, output: dict, rules: list[dict]) -> list[dict]:
        results = []
        raw = output.get("raw_response", "")
        answer = output.get("final_answer", "")
        is_stub = output.get("is_stub", False)
        exit_code = output.get("exit_code")
        execution = output.get("execution", {}) or {}

        # Always add an execution-integrity check so robustness reflects runtime quality.
        exec_passed = execution.get("robust_execution", True)
        exec_detail = execution.get("robust_detail", "execution stable")
        results.append({
            "check_id": "r_exec",
            "check_type": "execution_integrity",
            "passed": exec_passed,
            "detail": exec_detail,
        })

        for rule in rules:
            check_type = rule.get("check_type", "")
            passed = True
            detail = ""

            if check_type == "not_empty":
                passed = bool(answer or raw) and not is_stub
                if passed and output.get("incomplete_response"):
                    passed = False
                    detail = "response incomplete — model asked for input mid-task"
                else:
                    detail = "has output" if passed else ("stub/no output" if is_stub else "empty")
            elif check_type == "no_exception":
                passed = not re.search(r"(Traceback|Exception:|Error:|TimeoutError|FAILED)", raw)
                detail = "no exception" if passed else "exception/error detected"
                if exit_code is not None and exit_code != 0:
                    passed = False
                    detail = f"exit code {exit_code}"
                if execution and not execution.get("runtime_ok", True):
                    passed = False
                    detail = execution.get("failure_reason", "runtime degraded")
            elif check_type in ("doc_coverage", "param_valid", "example_match", "logic_coherent"):
                if is_stub:
                    # For no_code / stub runs: evaluate SKILL.md documentation coverage instead
                    skill_md = self.skill_path / "SKILL.md"
                    if skill_md.exists():
                        doc_text = skill_md.read_text(encoding="utf-8")
                        prompt_keywords = re.findall(r"\w{3,}", _tc.get("prompt", ""))
                        # Check how many prompt keywords appear in the doc
                        kw_hits = sum(1 for kw in prompt_keywords if kw.lower() in doc_text.lower())
                        coverage = kw_hits / max(len(prompt_keywords), 1)
                        if check_type == "doc_coverage":
                            passed = coverage >= 0.3
                            detail = f"doc keyword coverage {coverage:.0%} ({'✓' if passed else '✗'})"
                        elif check_type == "param_valid":
                            # Check if SKILL.md has any parameter-like content
                            has_params = bool(re.search(
                                r"(##\s*(parameters?|参数|usage)|[`\|]\s*\w+\s*\|)", doc_text, re.IGNORECASE
                            ))
                            passed = has_params
                            detail = "has param docs" if passed else "no param documentation"
                        elif check_type == "logic_coherent":
                            # Basic coherence: has multiple sections and substantial content
                            section_count = len(re.findall(r"^##\s", doc_text, re.MULTILINE))
                            passed = section_count >= 2 and len(doc_text) > 500
                            detail = f"{section_count} sections, {len(doc_text)} chars"
                        else:  # example_match
                            has_examples = bool(re.search(
                                r"##\s*(examples?|示例|running)", doc_text, re.IGNORECASE
                            ))
                            passed = has_examples
                            detail = "has example section" if passed else "no examples"
                    else:
                        passed = False
                        detail = "SKILL.md not found"
                else:
                    # For code execution: check if output is meaningful
                    passed = not is_stub and len(answer or raw) > 30
                    detail = "output meaningful" if passed else ("is stub" if is_stub else "output too short")
            else:
                passed = True
                detail = "check type not implemented (default pass)"

            results.append({
                "check_id": rule.get("check_id", ""),
                "check_type": check_type,
                "passed": passed,
                "detail": detail,
            })
        return results

    async def _eval_correctness(self, tc: dict, output: dict,
                                rules: list[dict]) -> tuple[list[dict], int]:
        """v5 §5.5: Programmatic validation first for deterministic/workflow profiles.

        Priority:
        1. Programmatic field/type check (deterministic/workflow).
        2. Batch LLM Judge via subprocess (token-isolated).
        3. Rule-based heuristic fallback.
        Returns (results, total_tokens).
        """
        results = []
        total_tokens = 0
        output_text = output.get("final_answer", "") or output.get("raw_response", "")
        is_stub = output.get("is_stub", False)
        has_llm = self._has_api_key() or (self._detect_openai_compat() is not None)
        use_llm = has_llm and not is_stub

        # Try programmatic field check for structured output profiles
        prog_result = None
        if self.profile in PROGRAMMATIC_FIRST_PROFILES and output_text and not is_stub:
            prog_result = self._programmatic_check(tc, output_text)

        # Use batch judge via subprocess for token isolation (v6 improvement)
        if use_llm and rules:
            batch_results, batch_tokens = await self._batch_judge_via_worker(
                output_text, rules, tc.get("prompt", ""), tc.get("expected_behavior", "")
            )
            if batch_results:
                total_tokens = batch_tokens
                for i, rule in enumerate(rules):
                    if i < len(batch_results):
                        br = batch_results[i]
                        score_levels = rule.get("score_levels", {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0})
                        level = br.get("level", "不满足")
                        results.append({
                            "assertion_id": rule.get("assertion_id", ""),
                            "criterion": rule.get("criterion", ""),
                            "score": br.get("score", 0.0),
                            "level": level,
                            "reasoning": br.get("reasoning", ""),
                            "tokens": 0,
                            "judge_duration_s": 0,
                            "eval_method": br.get("eval_method", "batch_judge"),
                            "needs_human_review": br.get("eval_method") == "batch_judge" and 0.3 <= br.get("score", 0) <= 0.7,
                        })
                    else:
                        results.append(self._rule_based_result(rule, output_text, output))
                return results, total_tokens

        # Fallback to original per-criterion evaluation
        for rule in rules:
            criterion = rule.get("criterion", "")
            score_levels = rule.get("score_levels", {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0})
            t0 = time.monotonic()

            # If programmatic check passed confidently, skip LLM judge for this criterion
            if prog_result and prog_result.get("confidence", 0) >= 0.7:
                score = prog_result["score"]
                reasoning = prog_result["reasoning"]
                tokens = 0
                eval_method = "programmatic"
            elif use_llm:
                guidance = rule.get("scoring_guidance") or (
                    f"你是一名严格的 Skill 评测员。请根据以下评分标准评估输出。\n\n"
                    f"【测试用例】\nPrompt: {tc.get('prompt','')[:200]}\nExpected: {tc.get('expected_behavior','')[:200]}\n\n"
                    f"【待评估输出】\n{output_text[:500]}\n\n"
                    f"【评分标准】\n{criterion}\n\n"
                    f"评分方式：完全满足(1.0) / 部分满足(0.5) / 不满足(0.0)\n"
                    f"要求：严格按评分标准，不打同情分；字段缺失或类型错误直接给0分"
                )
                try:
                    score, reasoning, tokens = await self.judge.score_correctness(
                        output_text, criterion, guidance, score_levels
                    )
                    total_tokens += tokens
                    eval_method = "llm_judge"
                except Exception as exc:  # pylint: disable=broad-except
                    score, reasoning, tokens = 0.5, f"Judge失败: {str(exc)[:80]}", 0
                    eval_method = "llm_judge_failed"
            else:
                score, reasoning = self._score_locally(output_text, criterion, output)
                tokens = 0
                eval_method = "rule_based"

            judge_dur = round(time.monotonic() - t0, 3)
            
            def make_level_key(captured_score, captured_levels):
                return min(captured_levels.keys(), key=lambda k: abs(captured_levels[k] - captured_score))
            
            level = make_level_key(score, score_levels)

            # v5 §5.5: flag low-confidence for human review
            needs_review = eval_method == "llm_judge" and 0.3 <= score <= 0.7

            results.append({
                "assertion_id": rule.get("assertion_id", ""),
                "criterion": criterion,
                "score": score,
                "level": level,
                "reasoning": reasoning,
                "tokens": tokens,
                "judge_duration_s": judge_dur,
                "eval_method": eval_method,
                "needs_human_review": needs_review,
            })
        return results, total_tokens

    def _rule_based_result(self, rule: dict, output_text: str, output: dict) -> dict:
        """Generate rule-based result for a single criterion."""
        score_levels = rule.get("score_levels", {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0})
        score, reasoning = self._score_locally(output_text, rule.get("criterion", ""), output)
        level = min(score_levels.keys(), key=lambda k: abs(score_levels[k] - score))
        return {
            "assertion_id": rule.get("assertion_id", ""),
            "criterion": rule.get("criterion", ""),
            "score": score,
            "level": level,
            "reasoning": reasoning,
            "tokens": 0,
            "judge_duration_s": 0,
            "eval_method": "rule_based",
            "needs_human_review": False,
        }

    async def _batch_judge_via_worker(
        self, output_text: str, rules: list[dict], prompt: str = "", expected: str = ""
    ) -> tuple[list[dict], int]:
        """v6: Batch judge via subprocess for token isolation.

        Returns (results, total_tokens). Empty results if worker fails.
        """
        worker_path = Path(__file__).parent.parent / "judge_worker.py"
        if not worker_path.exists():
            self.log.warning("layer4.judge_worker_missing", path=str(worker_path))
            return [], 0

        # Prepare request
        settings = self.judge.settings
        request = {
            "output": output_text,
            "criteria": [
                {
                    "assertion_id": r.get("assertion_id", ""),
                    "criterion": r.get("criterion", ""),
                    "score_levels": r.get("score_levels", {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}),
                }
                for r in rules
            ],
            "prompt": prompt,
            "expected": expected,
            "settings": {
                "anthropic_api_key": settings.anthropic_api_key,
                "openai_api_key": settings.openai_api_key,
                "openai_base_url": settings.openai_base_url,
                "judge_model": self.judge._judge_model,  # pylint: disable=protected-access
            },
        }

        judge_max_retries = 1
        judge_backoff_s = 2.0

        for attempt in range(judge_max_retries + 1):
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(worker_path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=json.dumps(request, ensure_ascii=False).encode()),
                    timeout=60.0,
                )

                if proc.returncode != 0:
                    self.log.warning("layer4.judge_worker_failed", returncode=proc.returncode,
                                    stderr=stderr.decode()[:200], attempt=attempt + 1)
                    if attempt < judge_max_retries:
                        await asyncio.sleep(judge_backoff_s)
                        continue
                    return [], 0

                response = json.loads(stdout.decode())
                results = response.get("results", [])
                tokens = response.get("total_tokens", 0)

                self.log.info("layer4.batch_judge_success", criteria_count=len(rules), tokens=tokens)
                return results, tokens

            except asyncio.TimeoutError:
                self.log.warning("layer4.judge_worker_timeout", attempt=attempt + 1)
                if attempt < judge_max_retries:
                    await asyncio.sleep(judge_backoff_s)
                    continue
                return [], 0
            except Exception as exc:  # pylint: disable=broad-except
                self.log.warning("layer4.judge_worker_error", error=str(exc)[:100], attempt=attempt + 1)
                if attempt < judge_max_retries:
                    await asyncio.sleep(judge_backoff_s)
                    continue
                return [], 0

        return [], 0

    def _programmatic_check(self, tc: dict, output_text: str) -> dict | None:
        """v5 §5.5: programmatic validation for deterministic/workflow outputs.

        Checks JSON field existence and basic type correctness.
        Returns None if output is not structured JSON.
        """
        # Try to extract JSON from output
        json_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", output_text)
        if not json_match:
            return None
        try:
            parsed = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            return None

        expected = tc.get("expected_behavior", "")
        # Extract expected field names from the expected_behavior text (simple heuristic)
        expected_fields = re.findall(r"[`'\"](\w+)[`'\"]", expected)

        if not expected_fields:
            return None

        if isinstance(parsed, dict):
            present = [f for f in expected_fields if f in parsed]
            coverage = len(present) / len(expected_fields)
            if coverage == 1.0:
                return {"score": 1.0, "reasoning": f"程序化校验: 所有字段存在 {present}", "confidence": 0.9}
            if coverage >= 0.5:
                missing = [f for f in expected_fields if f not in parsed]
                return {"score": 0.5, "reasoning": f"程序化校验: 缺失字段 {missing}", "confidence": 0.85}
            return {"score": 0.0, "reasoning": f"程序化校验: 大多数字段缺失，仅有 {present}", "confidence": 0.9}

        # Array output — check it's non-empty and first item has expected fields
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            present = [f for f in expected_fields if f in parsed[0]]
            coverage = len(present) / len(expected_fields)
            score = min(1.0, coverage)
            return {"score": score,
                    "reasoning": f"程序化校验: 数组首项字段覆盖率 {coverage:.0%}",
                    "confidence": 0.85}

        return None

    def _score_locally(self, output_text: str, criterion: str, output: dict) -> tuple[float, str]:
        """Rule-based correctness scoring when LLM judge is unavailable."""
        # For no_code / stub skills: evaluate SKILL.md content against criterion instead of execution output
        if output.get("is_stub"):
            skill_content = output.get("skill_content", "")
            if skill_content and len(skill_content) > 100:
                crit_tokens = set(re.split(r"[\W_]+", criterion.lower())) - {"", "的", "是", "在", "和", "或", "doc"}
                doc_tokens = set(re.split(r"[\W_]+", skill_content.lower()))
                overlap = crit_tokens & doc_tokens
                overlap_ratio = len(overlap) / max(len(crit_tokens), 1)
                doc_len = len(skill_content)
                if overlap_ratio >= 0.5:
                    return 1.0, f"no_code文档评分: SKILL.md关键词覆盖率高（{len(overlap)}/{len(crit_tokens)}，文档{doc_len}字符）"
                if overlap_ratio >= 0.25:
                    return 0.6, f"no_code文档评分: SKILL.md关键词部分覆盖（{len(overlap)}/{len(crit_tokens)}，文档{doc_len}字符）"
                if doc_len >= 2000:
                    return 0.5, f"no_code文档评分: SKILL.md内容丰富（{doc_len}字符）但与标准关键词重叠少"
                return 0.3, f"no_code文档评分: SKILL.md内容较少（{doc_len}字符），关键词匹配低"
            return 0.0, "本地执行失败或无输出，且无 SKILL.md 内容 — 无法评估"

        if not output_text or output_text.startswith("["):
            return 0.0, "本地执行失败或无输出 — 无法评估"

        length = len(output_text.strip())
        if length < 20:
            return 0.0, f"输出过短（{length} 字符），不满足任何标准"

        # Check for exception/error patterns
        if re.search(r"(Traceback|Exception|Error:|FAILED|fatal)", output_text):
            return 0.2, "输出含错误/异常信息"

        # Keyword overlap between criterion and output
        crit_tokens = set(re.split(r"[\W_]+", criterion.lower())) - {"", "的", "是", "在", "和", "或"}
        out_tokens  = set(re.split(r"[\W_]+", output_text.lower()))
        overlap = crit_tokens & out_tokens
        overlap_ratio = len(overlap) / max(len(crit_tokens), 1)

        if overlap_ratio >= 0.5:
            return 1.0, f"规则评分: 输出与标准关键词高度匹配（{len(overlap)}/{len(crit_tokens)}）"
        if overlap_ratio >= 0.25:
            return 0.5, f"规则评分: 输出与标准部分匹配（{len(overlap)}/{len(crit_tokens)}）"

        # Structural quality bonus
        has_structure = bool(re.search(r"(\n[-*•]|\d+\.|```|##|{|}|score|grade)", output_text))
        if length >= 200 and has_structure:
            return 0.5, f"规则评分: 输出详细有结构（{length}字符），但与标准关键词重叠少"
        if length >= 100:
            return 0.4, f"规则评分: 输出存在（{length}字符），与标准关键词重叠少"
        return 0.3, f"规则评分: 输出较短（{length}字符），与标准关键词重叠少"

    def _agg_robustness(self, results: list[dict], rules: list[dict]) -> float:
        if not results:
            return 1.0
        weights = {r.get("check_id"): r.get("weight", 1.0) for r in rules}
        total_w = sum(weights.get(r["check_id"], 1.0) for r in results)
        if total_w == 0:
            return 1.0
        score = sum((1.0 if r["passed"] else 0.0) * weights.get(r["check_id"], 1.0) for r in results)
        return score / total_w

    def _build_execution_record(self, run_mode: str, status: str, output: dict) -> dict:
        """Build normalized execution metadata for per-case diagnostics."""
        attempts = output.get("execution_attempts", []) or []
        provider = output.get("provider", "unknown")

        if provider == "claude_cli":
            route = "claude_cli"
            route_label = "claude CLI 执行"
        elif provider == "openai":
            route = "openapi_direct"
            route_label = "openapi 直接执行"
        elif provider == "anthropic":
            route = "anthropic_direct"
            route_label = "anthropic 直接执行"
        elif provider == "local":
            route = "local_entry"
            route_label = "本地入口执行"
        else:
            route = "unknown"
            route_label = "未知执行路径"

        failure_tags: list[str] = []
        if status == "timeout":
            failure_tags.append("tc_timeout")
        if status == "failed":
            failure_tags.append("invoke_failed")
        if output.get("is_stub"):
            failure_tags.append("stub_output")
        if output.get("incomplete_response"):
            failure_tags.append("incomplete_response")
        if output.get("api_error"):
            failure_tags.append("api_error")
        if output.get("tooling_failed"):
            failure_tags.append("tooling_failed")
        sim_note = str(output.get("simulation_note") or "")
        if output.get("warning") == "max tool iterations reached" or "max_iterations=" in sim_note:
            failure_tags.append("max_iterations_reached")
            match = re.search(r"max_iterations\s*=\s*(\d+)", sim_note)
            if match:
                failure_tags.append(f"max_iterations_{match.group(1)}")

        # Detect empty output: raw_response and final_answer are both empty/blank
        raw_response = (output.get("raw_response") or "").strip()
        final_answer = (output.get("final_answer") or "").strip()
        effective_output = raw_response or final_answer
        min_meaningful_length = 20  # responses shorter than this are likely not useful

        if not effective_output:
            failure_tags.append("empty_output")
        elif len(effective_output) < min_meaningful_length:
            failure_tags.append("short_output")

        # Detect non-zero exit code as a strong failure signal
        exit_code = output.get("exit_code")
        if exit_code is not None and exit_code != 0:
            failure_tags.append("nonzero_exit_code")

        # Detect error-like responses (model returned an error message instead of real output)
        if effective_output:
            lower_output = effective_output.lower()
            error_indicators = [
                "error:", "error：", "failed to", "cannot ", "unable to",
                "exception", "traceback", "permission denied",
                "api error", "authentication_error", "unauthorized",
                "服务未授权", "认证失败", "401", "403", "500",
            ]
            # Short outputs with error keywords are almost certainly failures;
            # longer outputs may contain error keywords in legitimate context,
            # so only flag if the output is predominantly an error message.
            max_error_length = 500
            if any(indicator in lower_output for indicator in error_indicators) and len(effective_output) < max_error_length:
                failure_tags.append("error_in_output")

        failure_reason = (
            output.get("tooling_error")
            or output.get("api_error")
            or ""
        )

        # empty_output, error_in_output, and nonzero_exit_code are hard failures
        has_output_failure = bool({"empty_output", "error_in_output", "nonzero_exit_code"} & set(failure_tags))
        runtime_ok = (status == "success"
                      and not output.get("is_stub", False)
                      and "api_error" not in failure_tags
                      and not has_output_failure)
        robust_execution = runtime_ok and "incomplete_response" not in failure_tags and "short_output" not in failure_tags
        robust_detail = "execution stable" if robust_execution else (
            f"execution degraded: {', '.join(failure_tags)}"
        )

        return {
            "run_mode": run_mode,
            "route": route,
            "route_label": route_label,
            "provider": provider,
            "attempts": attempts,
            "status": status,
            "failure_tags": failure_tags,
            "failure_reason": failure_reason,
            "runtime_ok": runtime_ok,
            "robust_execution": robust_execution,
            "robust_detail": robust_detail,
        }

    def _build_execution_breakdown(self, per_case: list[dict]) -> dict:
        """Aggregate execution-path and failure stats across test cases."""
        route_counts: dict[str, int] = {}
        failure_counts: dict[str, int] = {}
        failed_cases: list[dict] = []

        for case in per_case:
            with_run = case.get("with", {}) or {}
            execution = with_run.get("execution", {}) or {}
            route = execution.get("route", "unknown")
            route_counts[route] = route_counts.get(route, 0) + 1

            for tag in execution.get("failure_tags", []):
                failure_counts[tag] = failure_counts.get(tag, 0) + 1

            if execution.get("failure_tags"):
                failed_cases.append({
                    "tc_id": case.get("tc_id"),
                    "route": route,
                    "failure_tags": execution.get("failure_tags", []),
                    "failure_reason": execution.get("failure_reason", ""),
                })

        return {
            "route_counts": route_counts,
            "failure_counts": failure_counts,
            "failed_cases": failed_cases,
        }

    def _agg_correctness(self, results: list[dict], rules: list[dict]) -> float:
        if not results:
            return 0.5
        weights = {r.get("assertion_id"): r.get("weight", 1.0) for r in rules}
        total_w = sum(weights.get(r["assertion_id"], 1.0) for r in results)
        if total_w == 0:
            return 0.5
        score = sum(r["score"] * weights.get(r["assertion_id"], 1.0) for r in results)
        return score / total_w
