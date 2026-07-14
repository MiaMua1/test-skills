"""Layer 3: Test case generation + dynamic scoring criteria (must be atomic)."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

from evaluator.config import get_score_profiles, get_settings
from evaluator.models.exceptions import EvaluationError
from evaluator.models.skill import SkillInfo

logger = structlog.get_logger()


class Layer3TestGen:
    """Layer 3: Generates evals.json and scoring_criteria.json atomically.

    Both files share the same eval_id and are written to the same directory.
    They must be generated in a single batch — never split across calls.

    Supports two optional overrides:
    - evals_file: Path to a local evals.json (or JSON array of test cases).
      When provided, skips auto-generation and uses these test cases directly.
    - criteria_file: Path to a local scoring_criteria.json (or JSON array of
      criteria entries). When provided, uses the file's scoring rules.
      profile_weight_snapshot is ALWAYS re-stamped from live config regardless
      of what the file contains, to satisfy the weight-sum invariant.
    """

    layer_number = 3
    layer_name = "layer3_testgen"

    def __init__(
        self,
        skill_info: SkillInfo,
        storage_base: Path,
        evals_file: Path | str | None = None,
        criteria_file: Path | str | None = None,
        skip_baseline: bool = False,
        with_baseline: bool = False,
        max_cases: int | None = None,
    ) -> None:
        self.skill_info = skill_info
        self.skill_path = skill_info.skill_path
        self.profile = skill_info.eval_profile.value
        self.weights = get_score_profiles(with_baseline)[self.profile]
        self.storage_base = storage_base
        self.settings = get_settings()
        self.log = logger.bind(layer=self.layer_name, skill=skill_info.metadata.name)

        self.evals_file = Path(evals_file).resolve() if evals_file else None
        self.criteria_file = Path(criteria_file).resolve() if criteria_file else None
        self.skip_baseline = skip_baseline
        self.max_cases = max_cases

        if self.evals_file and not self.evals_file.exists():
            raise EvaluationError(f"evals_file not found: {self.evals_file}")
        if self.criteria_file and not self.criteria_file.exists():
            raise EvaluationError(f"criteria_file not found: {self.criteria_file}")

    async def run(self, eval_id: str) -> dict:
        """Generate (or load) test cases and scoring criteria for this eval_id.

        Priority logic:
        - evals_file provided   → load from file, mark source="provided"
        - evals_file absent     → auto-generate from SKILL.md (reuse if cached)
        - criteria_file provided → load from file, re-stamp profile_weight_snapshot
        - criteria_file absent  → auto-generate from test cases

        Returns:
            Dict with paths, coverage stats, and source provenance.
        """
        evals_dir = self.storage_base / "evals" / self.skill_info.metadata.name / eval_id
        evals_dir.mkdir(parents=True, exist_ok=True)

        evals_path = evals_dir / "evals.json"
        criteria_path = evals_dir / "scoring_criteria.json"

        t_start = time.monotonic()

        # ── Step 1: Resolve test cases ─────────────────────────────────────
        if self.evals_file:
            test_cases, evals_data = self._load_evals_from_file(eval_id)
            evals_source = str(self.evals_file)
            self.log.info("layer3.evals_loaded_from_file",
                          path=evals_source, tc_count=len(test_cases))
        else:
            # Reuse cached files when eval_id matches (avoids redundant LLM calls)
            if evals_path.exists() and criteria_path.exists():
                existing = json.loads(evals_path.read_text(encoding="utf-8"))
                if existing.get("eval_id") == eval_id:
                    self.log.info("layer3.reusing_existing", eval_id=eval_id)
                    return {
                        "status": "reused",
                        "evals_source": "auto",
                        "criteria_source": "auto",
                        "evals_path": str(evals_path),
                        "criteria_path": str(criteria_path),
                        "coverage": existing.get("coverage", {}),
                    }
            skill_content = self._read_skill_md()
            test_cases = self._generate_test_cases(skill_content, eval_id)
            evals_data = self._build_evals_data(eval_id, test_cases)
            evals_source = "auto"

        # Write evals.json
        evals_path.write_text(json.dumps(evals_data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        self.log.info("layer3.evals_saved", eval_id=eval_id,
                      tc_count=len(test_cases), source=evals_source)

        # ── Step 2: Resolve scoring criteria ──────────────────────────────
        if self.criteria_file:
            criteria_data = self._load_criteria_from_file(eval_id, test_cases)
            criteria_source = str(self.criteria_file)
            self.log.info("layer3.criteria_loaded_from_file", path=criteria_source)
        elif self.evals_file:
            # User uploaded test cases but provided no criteria:
            # check whether any TC has a non-empty rubric; if not, use the default fallback
            has_rubric = any(tc.get("correctness_rubric") for tc in test_cases)
            if has_rubric:
                criteria_data = self._build_scoring_criteria(eval_id, test_cases)
                criteria_source = "auto_from_evals_rubric"
            else:
                criteria_data = self._build_default_scoring_criteria(eval_id, test_cases)
                criteria_source = "default_fallback"
        else:
            criteria_data = self._build_scoring_criteria(eval_id, test_cases)
            criteria_source = "auto"

        # Atomic write — evals.json and scoring_criteria.json always in one batch
        criteria_path.write_text(json.dumps(criteria_data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        duration = round(time.monotonic() - t_start, 3)
        self.log.info("layer3.criteria_saved", eval_id=eval_id,
                      tc_count=len(test_cases), duration_s=duration,
                      evals_source=evals_source, criteria_source=criteria_source)

        return {
            "status": "provided" if self.evals_file else "generated",
            "evals_source": evals_source,
            "criteria_source": criteria_source,
            "evals_path": str(evals_path),
            "criteria_path": str(criteria_path),
            "coverage": evals_data["coverage"],
            "tc_count": len(test_cases),
            "duration_s": duration,
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _read_skill_md(self) -> str:
        skill_md = self.skill_path / "SKILL.md"
        if skill_md.exists():
            return skill_md.read_text(encoding="utf-8")
        return ""

    def _read_readme(self) -> str:
        """Read README.md (or README) from the skill directory if it exists."""
        for name in ("README.md", "README", "readme.md", "Readme.md", "USAGE.md", "usage.md"):
            readme = self.skill_path / name
            if readme.exists():
                return readme.read_text(encoding="utf-8")
        return ""

    def _build_evals_data(self, eval_id: str, test_cases: list[dict]) -> dict:
        """Build the evals.json payload from a list of test case dicts."""
        coverage = {
            "p0_count": sum(1 for t in test_cases if t.get("priority") == "P0"),
            "p1_count": sum(1 for t in test_cases if t.get("priority") == "P1"),
            "p2_count": sum(1 for t in test_cases if t.get("priority") == "P2"),
        }
        return {
            "skill_name": self.skill_info.metadata.name,
            "eval_id": eval_id,
            "skill_type": (
                self.skill_info.metadata.type.value
                if self.skill_info.metadata.type else "unknown"
            ),
            "eval_profile": self.profile,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "coverage": coverage,
            "test_cases": test_cases,
        }

    def _default_robustness_checks(self) -> list[dict]:
        """Return default robustness checks based on whether the skill has code."""
        if self.skill_info.has_code:
            return [
                {"description": "执行无异常抛出", "check_type": "no_exception"},
                {"description": "返回结果非空", "check_type": "not_empty"},
            ]
        return [
            {"description": "SKILL.md 覆盖该用例场景", "check_type": "doc_coverage"},
            {"description": "参数说明完整", "check_type": "param_valid"},
        ]

    def _normalize_test_case(self, raw: dict, index: int) -> dict:
        """Normalize a user-provided test case dict, filling in missing fields."""
        tc = dict(raw)
        tc.setdefault("id", f"tc_{index:03d}")
        tc.setdefault("priority", "P0")
        tc["source"] = "manual"
        tc.setdefault("expected_behavior", "")
        tc.setdefault("context", {})
        tc.setdefault("robustness_checks", self._default_robustness_checks())
        tc.setdefault("correctness_rubric", [])

        # Ensure baseline_prompt exists for profiles that need delta scoring.
        # When skip_baseline=True (user provided both evals + criteria), skip delta.
        if "baseline_prompt" not in tc:
            needs_delta = self.weights.delta_max > 0 and not self.skip_baseline
            prompt = tc.get("prompt", "")
            tc["baseline_prompt"] = (
                f"不使用任何 skill 或工具，仅凭通用 LLM 能力完成以下任务（对照组）：{prompt}"
                if needs_delta else None
            )
        return tc

    def _load_evals_from_file(self, eval_id: str) -> tuple[list[dict], dict]:
        """Load and normalize test cases from a local file.

        Accepts:
        - Full evals.json format: ``{"test_cases": [...], ...}``
        - Bare JSON array of test case objects: ``[{...}, ...]``

        Always re-stamps eval_id, generated_at, and marks source="manual".
        """
        try:
            raw = json.loads(self.evals_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise EvaluationError(f"Failed to read evals_file {self.evals_file}: {exc}") from exc

        if isinstance(raw, list):
            raw_cases = raw
        elif isinstance(raw, dict) and "test_cases" in raw:
            raw_cases = raw["test_cases"]
        else:
            raise EvaluationError(
                f"Invalid evals_file format: expected JSON array or object with 'test_cases' key "
                f"in {self.evals_file}"
            )

        if not raw_cases:
            raise EvaluationError(f"evals_file contains no test cases: {self.evals_file}")

        test_cases = [
            self._normalize_test_case(tc, i + 1)
            for i, tc in enumerate(raw_cases)
            if isinstance(tc, dict) and tc.get("prompt")
        ]

        if not test_cases:
            raise EvaluationError(
                f"evals_file has no valid test cases (each entry must have a 'prompt' field): "
                f"{self.evals_file}"
            )

        evals_data = self._build_evals_data(eval_id, test_cases)
        evals_data["evals_source"] = str(self.evals_file)
        return test_cases, evals_data

    def _load_criteria_from_file(self, eval_id: str, test_cases: list[dict]) -> dict:
        """Load scoring criteria from a local file as GLOBAL standards applied to ALL test cases.

        The file defines ONE shared evaluation method and metrics — no tc_id matching needed.
        The same criteria are applied uniformly to every test case.

        Supported formats (auto-detected):
        1. Flat list of criterion objects (simplest, recommended):
           ``[{"criterion": "...", "weight": 2.0, "scoring_guidance": "..."}, ...]``

        2. Object with a criteria key:
           ``{"criteria": [...]}``  or  ``{"correctness_scoring": [...]}``

        3. Object with optional robustness_checks key:
           ``{"criteria": [...], "robustness_checks": [...]}``

        4. Legacy per-tc format (``{"criteria_by_tc": [...]}``): still accepted for
           backward-compatibility; each entry is treated as-is with weight_snapshot re-stamped.

        profile_weight_snapshot is ALWAYS taken from live config (never trusted from file).
        """
        try:
            raw = json.loads(self.criteria_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise EvaluationError(
                f"Failed to read criteria_file {self.criteria_file}: {exc}"
            ) from exc

        # ── Detect format and extract global criteria ──────────────────────
        global_criteria: list[dict] = []
        custom_robustness: list[dict] | None = None
        is_legacy_per_tc = False

        if isinstance(raw, list):
            # Format 1: bare list — could be global criteria OR legacy per-tc entries
            if raw and isinstance(raw[0], dict) and "tc_id" in raw[0]:
                # Looks like legacy per-tc
                is_legacy_per_tc = True
            else:
                global_criteria = raw

        elif isinstance(raw, dict):
            if "criteria_by_tc" in raw:
                is_legacy_per_tc = True
            else:
                # Format 2/3: object with criteria key
                global_criteria = (
                    raw.get("criteria")
                    or raw.get("correctness_scoring")
                    or raw.get("correctness_criteria")
                    or []
                )
                custom_robustness = raw.get("robustness_checks") or raw.get("robustness_criteria")

        # ── Legacy per-tc path (backward-compat) ──────────────────────────
        if is_legacy_per_tc:
            self.log.warning(
                "layer3.criteria_file_legacy_format",
                hint="File uses legacy per-tc format (criteria_by_tc / tc_id entries). "
                     "Consider switching to the global format: a flat list of criterion objects.",
                criteria_file=str(self.criteria_file),
            )
            return self._load_legacy_criteria_from_file(eval_id, test_cases, raw)

        # ── Global criteria path (primary) ────────────────────────────────
        if not global_criteria:
            self.log.warning(
                "layer3.criteria_file_empty",
                criteria_file=str(self.criteria_file),
                hint="No criteria found in file; falling back to default scoring criteria.",
            )
            return self._build_default_scoring_criteria(eval_id, test_cases,
                                                        criteria_source=str(self.criteria_file))

        self.log.info(
            "layer3.criteria_file_global",
            criteria_count=len(global_criteria),
            criteria_file=str(self.criteria_file),
        )
        return self._apply_global_criteria(
            eval_id, test_cases, global_criteria,
            custom_robustness=custom_robustness,
            criteria_source=str(self.criteria_file),
        )

    def _apply_global_criteria(
        self,
        eval_id: str,
        test_cases: list[dict],
        global_criteria: list[dict],
        custom_robustness: list[dict] | None = None,
        criteria_source: str = "global_file",
    ) -> dict:
        """Apply the same set of criteria to every test case (global evaluation method)."""
        snapshot = self.weights.as_snapshot()

        # Normalise correctness criteria entries
        correctness_scoring: list[dict] = []
        for i, cr in enumerate(global_criteria):
            if not isinstance(cr, dict) or not cr.get("criterion"):
                continue
            correctness_scoring.append({
                "assertion_id": f"c_{i+1:03d}",
                "criterion": cr["criterion"],
                "weight": float(cr.get("weight", 1.0)),
                "score_levels": cr.get(
                    "score_levels",
                    {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0},
                ),
                "scoring_guidance": cr.get(
                    "scoring_guidance",
                    f"评估输出是否满足：「{cr['criterion']}」。"
                    "完全满足（1.0）：输出清晰准确地满足该标准；"
                    "部分满足（0.5）：输出部分满足但有明显遗漏；"
                    "不满足（0.0）：输出完全不符合该标准。",
                ),
            })

        tc_snapshot = {
            "robust_max": snapshot["robust_max"],
            "correct_max": snapshot["correct_max"],
            "delta_max": snapshot["delta_max"],
        }
        needs_delta = snapshot["delta_max"] > 0

        criteria_by_tc: list[dict] = []
        for tc in test_cases:
            tc_id = tc.get("id", "")

            # Robustness: use custom_robustness from file, or fall back to TC's own checks
            if custom_robustness:
                robustness_scoring = [
                    {
                        "check_id": f"r_{i+1:03d}",
                        "description": rc.get("description", rc.get("check_type", "")),
                        "check_type": rc.get("check_type", "not_empty"),
                        "pass_score": 1.0,
                        "fail_score": 0.0,
                        "weight": float(rc.get("weight", 1.0)),
                    }
                    for i, rc in enumerate(custom_robustness)
                ]
            else:
                robustness_scoring = [
                    {
                        "check_id": f"r_{i+1:03d}",
                        "description": rc["description"],
                        "check_type": rc["check_type"],
                        "pass_score": 1.0,
                        "fail_score": 0.0,
                        "weight": 1.0,
                    }
                    for i, rc in enumerate(tc.get("robustness_checks", []))
                ]

            delta_scoring = {
                "delta_max": snapshot["delta_max"],
                "formula": f"max(0, with_correct - without_correct + 0.5) × {snapshot['delta_max']}",
                "guidance": "对比有无 skill 时的正确性得分差值，持平得 50% delta 分",
            } if needs_delta else None

            criteria_by_tc.append({
                "tc_id": tc_id,
                "weight_snapshot": tc_snapshot,
                "robustness_scoring": robustness_scoring,
                "correctness_scoring": correctness_scoring,  # same for every TC
                "delta_scoring": delta_scoring,
            })

        return {
            "eval_id": eval_id,
            "skill_name": self.skill_info.metadata.name,
            "eval_profile": self.profile,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "profile_weight_snapshot": snapshot,
            "criteria_source": criteria_source,
            "criteria_by_tc": criteria_by_tc,
        }

    def _build_default_scoring_criteria(
        self, eval_id: str, test_cases: list[dict], criteria_source: str = "default_fallback"
    ) -> dict:
        """Build a simple generic scoring criteria when no criteria file is provided.

        Used when user uploads only an evals file (no criteria_file).
        Applies a sensible default rubric to every test case regardless of content.
        """
        default_criteria = [
            {
                "criterion": "输出内容与 prompt 要求相关，直接回应了用户的问题或任务",
                "weight": 3.0,
                "scoring_guidance": (
                    "判断输出内容是否与用户 prompt 直接相关。"
                    "完全满足（1.0）：输出直接针对 prompt 核心要求；"
                    "部分满足（0.5）：输出相关但有明显偏离或遗漏；"
                    "不满足（0.0）：输出与 prompt 无关或完全偏题。"
                ),
            },
            {
                "criterion": "输出结构完整，没有截断、报错或无意义占位内容",
                "weight": 2.0,
                "scoring_guidance": (
                    "判断输出是否完整、无异常。"
                    "完全满足（1.0）：输出完整，无错误信息；"
                    "部分满足（0.5）：输出存在但有截断或小错误；"
                    "不满足（0.0）：输出为空、报错或是无意义占位符。"
                ),
            },
            {
                "criterion": "输出质量达标（内容准确、逻辑清晰、无明显幻觉或矛盾）",
                "weight": 3.0,
                "scoring_guidance": (
                    "判断输出内容的质量。"
                    "完全满足（1.0）：内容准确、逻辑清晰；"
                    "部分满足（0.5）：内容基本正确但有小问题；"
                    "不满足（0.0）：内容明显错误、逻辑混乱或充斥幻觉。"
                ),
            },
            {
                "criterion": "格式符合场景需求（如 Markdown/JSON/代码块 使用恰当）",
                "weight": 2.0,
                "scoring_guidance": (
                    "判断输出格式是否恰当。"
                    "完全满足（1.0）：格式与场景完全匹配；"
                    "部分满足（0.5）：格式基本合适但有小问题；"
                    "不满足（0.0）：格式完全不符合场景要求。"
                ),
            },
        ]
        self.log.info(
            "layer3.using_default_criteria",
            tc_count=len(test_cases),
            criteria_source=criteria_source,
        )
        return self._apply_global_criteria(
            eval_id, test_cases, default_criteria,
            criteria_source=criteria_source,
        )

    def _load_legacy_criteria_from_file(
        self, eval_id: str, test_cases: list[dict], raw: dict | list
    ) -> dict:
        """Backward-compatible loader for the old per-tc criteria_by_tc format.

        Kept for compatibility; new uploads should use the global format.
        """
        if isinstance(raw, list):
            file_entries = raw
        else:
            file_entries = raw.get("criteria_by_tc", [])

        file_by_id: dict[str, dict] = {
            e["tc_id"]: e
            for e in file_entries
            if isinstance(e, dict) and "tc_id" in e
        }

        auto_data = self._build_scoring_criteria(eval_id, test_cases)
        auto_by_id: dict[str, dict] = {
            c["tc_id"]: c for c in auto_data["criteria_by_tc"]
        }

        snapshot = self.weights.as_snapshot()
        live_weight_snapshot = {
            "robust_max": snapshot["robust_max"],
            "correct_max": snapshot["correct_max"],
            "delta_max": snapshot["delta_max"],
        }

        matched_ids: set[str] = set()
        criteria_by_tc: list[dict] = []
        for tc in test_cases:
            tc_id = tc.get("id", "")
            if tc_id in file_by_id:
                entry = dict(file_by_id[tc_id])
                entry["weight_snapshot"] = live_weight_snapshot
                criteria_by_tc.append(entry)
                matched_ids.add(tc_id)
            else:
                criteria_by_tc.append(auto_by_id[tc_id])

        unmatched = set(file_by_id.keys()) - matched_ids
        if unmatched:
            self.log.warning(
                "layer3.criteria_file_unmatched_tc_ids",
                unmatched=sorted(unmatched),
                criteria_file=str(self.criteria_file),
            )

        return {
            "eval_id": eval_id,
            "skill_name": self.skill_info.metadata.name,
            "eval_profile": self.profile,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "profile_weight_snapshot": snapshot,
            "criteria_source": str(self.criteria_file),
            "criteria_by_tc": criteria_by_tc,
        }

    def _extract_triggers(self, description: str) -> list[str]:
        """Extract quoted trigger phrases from frontmatter description field.

        Handles patterns like:
          "Triggers on: 'foo', 'bar', ..." or "Use when: ..."
        Returns up to 8 distinct trigger strings.
        """
        triggers: list[str] = []
        # Match the "Triggers on: ..." block
        m = re.search(r"[Tt]riggers?\s+on[:\s]+(.+?)(?:\.\s|$)", description, re.DOTALL)
        if m:
            trigger_block = m.group(1)
            quoted = re.findall(r"['\u2018\u2019\u201c\u201d]([^'\u2018\u2019\u201c\u201d]{2,60})['\u2018\u2019\u201c\u201d]", trigger_block)
            for q in quoted:
                q = q.strip()
                if q and not q.lower().startswith("http"):
                    triggers.append(q)

        # Also extract "Use when ..." clause style triggers
        uw = re.search(r"[Uu]se when[:\s]+(.+?)(?:\.|$)", description, re.DOTALL)
        if uw:
            # Split on commas or "or"
            parts = re.split(r",\s*|(?:\bor\b)", uw.group(1))
            for p in parts:
                p = p.strip().rstrip(".")
                if 10 < len(p) < 80:
                    triggers.append(p)

        # Remove duplicate-ish triggers (case-insensitive)
        seen_lower: set[str] = set()
        deduped: list[str] = []
        for t in triggers:
            key = t.lower()
            if key not in seen_lower:
                seen_lower.add(key)
                deduped.append(t)

        # Sort: command-style triggers first (/xxx or CamelCase), then short Chinese, then others
        def _trigger_priority(t: str) -> int:
            if re.match(r"^/\w", t):
                return 0  # /pua, /cmd — highest priority
            if re.match(r"^[A-Z\u4e00-\u9fa5]{2,}[模式:]", t):
                return 1  # PUA模式, 阿里巴巴模式
            if len(t) <= 6 and any("\u4e00" <= c <= "\u9fa5" for c in t):
                return 2  # 加油, 别偷懒 — short Chinese
            return 3

        deduped.sort(key=_trigger_priority)
        return deduped[:10]

    def _build_trigger_scenarios(
        self,
        skill_name: str,
        triggers: list[str],
        description: str,
    ) -> list[tuple[str, str, list]]:
        """Build realistic P0 prompts using trigger phrases from the skill description.

        For no_code / behavior skills, the trigger IS the invocation — combining
        the trigger phrase with a realistic task creates an authentic test case.
        """
        del description
        if not triggers:
            return []

        task_templates = [
            "帮我实现一个用户登录功能，但需要支持 OAuth2 和密码两种方式",
            "这段代码有 Bug 导致线上报错，帮我定位并修复",
            "帮我写一份完整的技术方案文档，包含架构图和实施步骤",
            "这个接口超时问题已经发生 3 次了，请深入分析根因",
            "帮我优化这个查询的性能，目前每次需要 5 秒",
        ]
        scenarios: list[tuple[str, str, list]] = []
        for i, trigger in enumerate(triggers[:5]):
            task = task_templates[i % len(task_templates)]
            # Command-style triggers (e.g. /pua) are prefixed directly; others are natural language
            if re.match(r"^/\w", trigger) or re.match(r"^[A-Z]{2,}\w*[模式:]", trigger):
                prompt = f"{trigger} {task}"
            else:
                prompt = f"{trigger}，{task}"
            expected = (
                f"AI 加载 {skill_name} skill，切换为对应风格/模式，"
                f"以 {skill_name} 定义的约束和行为规范完成任务，"
                f"而非普通 AI 助手的默认风格"
            )
            scenarios.append((prompt, expected, []))

        return scenarios

    def _parse_frontmatter(self, content: str) -> tuple[str, dict]:
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not m:
            return content, {}
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        return content[m.end():], fm

    def _generate_test_cases(self, skill_content: str, _eval_id: str) -> list[dict]:
        """Generate P0/P1/P2 test cases from SKILL.md content (+ README when present)."""
        body, fm = self._parse_frontmatter(skill_content)
        skill_name = fm.get("name", self.skill_info.metadata.name)
        has_code = self.skill_info.has_code

        if self.max_cases is not None and self.max_cases > 0:
            total = self.max_cases
            # P0-heavy: ≤5 → all P0 (+ leftover P1); >5 → ~60% P0, ~25% P1, ~15% P2
            if total <= 5:
                p0_count = max(1, total - total // 3)
                p1_count = total - p0_count
                p2_count = 0
            else:
                p0_count = max(1, round(total * 0.6))
                p1_count = max(0, round(total * 0.25))
                p2_count = max(0, total - p0_count - p1_count)
            self.log.info("layer3.max_cases_override",
                          max_cases=total, p0=p0_count, p1=p1_count, p2=p2_count)
        else:
            p0_count = self.settings.layer3_p0_count
            p1_count = self.settings.layer3_p1_count
            p2_count = self.settings.layer3_p2_count

        cases = []
        idx = 1

        # Extract skill context for richer prompts
        examples = self._extract_examples(body)
        use_cases = self._extract_use_cases(body, skill_name)
        params = self._extract_params(body)
        description = fm.get("description", "") or self._extract_description(body)

        # Also read README for usage examples — README often contains clearer invocation patterns
        readme_content = self._read_readme()
        readme_body, _ = self._parse_frontmatter(readme_content) if readme_content else ("", {})
        readme_examples = self._extract_examples(readme_body) if readme_body else []
        readme_use_cases = self._extract_use_cases(readme_body, skill_name) if readme_body else []

        # For no_code / behavior skills: extract trigger phrases from description frontmatter
        triggers = self._extract_triggers(description) if (not has_code and description) else []

        seen_fingerprints: set[str] = set()

        def _accept(prompt_text: str) -> bool:
            """Return True only if this prompt is sufficiently distinct from all accepted ones."""
            tokens = set(re.split(r"[\s，。、：；！？（）\[\]]+", prompt_text.lower()))
            tokens -= {"使用", "处理", "skill", skill_name.lower(), "", "的", "在", "时", "和", "或", "以"}
            fp = frozenset(list(tokens)[:12])
            # Reject if >50% of key tokens overlap with any already-accepted fingerprint
            for seen in seen_fingerprints:
                if len(fp & seen) > max(1, len(fp) * 0.5):
                    return False
            seen_fingerprints.add(fp)
            return True

        # P0: Core functionality — priority order:
        #   1. Trigger-phrase scenarios (no_code skills only) — most authentic invocation
        #   2. Real examples from README (clearest usage documentation)
        #   3. Real examples from SKILL.md Examples section
        #   4. Use-case sentences from README / SKILL.md
        #   5. Fallback: built from params + description
        added_p0 = 0
        p0_candidates: list[tuple[str, str, list]] = []

        # 1. Trigger scenarios (no_code skills)
        if triggers:
            p0_candidates += self._build_trigger_scenarios(skill_name, triggers, description)

        # 2. README examples
        for ex in readme_examples:
            p0_candidates.append((ex["prompt"], ex.get("expected", f"{skill_name} 完成核心功能"), ex.get("rubric", [])))

        # 3. SKILL.md examples
        for ex in examples:
            p0_candidates.append((ex["prompt"], ex.get("expected", f"{skill_name} 完成核心功能"), ex.get("rubric", [])))

        # 4. Use-cases from README then SKILL.md
        for uc in readme_use_cases + use_cases:
            p0_candidates.append((uc, f"{skill_name} 准确完成：{uc[:80]}，输出有意义的结构化结果", []))

        # 5. Fallback: generic + param-based scenarios (only if no triggers found)
        if not triggers and description:
            p0_candidates.append((
                f"请演示 {skill_name} 的完整核心功能：{description[:150]}",
                f"{skill_name} 完成核心功能，返回完整、准确的输出",
                [],
            ))
        p0_candidates += self._build_core_scenarios(skill_name, params, description)

        for prompt, expected, rubric in p0_candidates:
            if added_p0 >= p0_count:
                break
            if _accept(prompt):
                cases.append(self._make_tc(idx, "P0", prompt, expected, has_code, skill_name, extra_rubric=rubric))
                idx += 1
                added_p0 += 1

        # P1: Edge cases
        param_names = [p["name"] for p in params if p.get("name")]
        if triggers:
            edge_scenarios = self._build_trigger_edge_scenarios(skill_name, triggers, description)
        else:
            edge_scenarios = self._build_edge_scenarios(skill_name, param_names, description)
        added_p1 = 0
        for prompt in edge_scenarios:
            if added_p1 >= p1_count:
                break
            if _accept(prompt):
                cases.append(self._make_tc(idx, "P1", prompt,
                                           f"{skill_name} 优雅处理边界情况，不崩溃，给出合理或明确的反馈",
                                           has_code, skill_name))
                idx += 1
                added_p1 += 1

        # P2: Error handling / negative cases
        if triggers:
            error_scenarios = self._build_trigger_error_scenarios(skill_name, triggers, description)
        else:
            error_scenarios = self._build_error_scenarios(skill_name, param_names, description)
        added_p2 = 0
        for prompt in error_scenarios:
            if added_p2 >= p2_count:
                break
            if _accept(prompt):
                cases.append(self._make_tc(idx, "P2", prompt,
                                           f"{skill_name} 在无效输入时给出明确错误提示或安全降级，不产生误导性输出",
                                           has_code, skill_name))
                idx += 1
                added_p2 += 1

        return cases

    def _extract_use_cases(self, body: str, skill_name: str) -> list[str]:
        """Extract trigger keywords / use-case sentences from the skill body.

        Filters out markdown table rows and lines with heavy pipe usage to avoid
        picking up documentation tables as if they were user prompts.
        """
        del skill_name
        use_cases = []
        lines = body.split("\n")
        for line in lines:
            line = line.strip()
            if len(line) < 20:
                continue
            if line.startswith(("#", "```", "|")):
                continue
            # Skip lines that are clearly table rows (many pipe chars)
            if line.count("|") > 2:
                continue
            # Pick sentences that look like user stories / trigger descriptions
            if any(kw in line for kw in ["当用户", "用于", "使用场景", "Use when", "use when",
                                          "需要评测", "需要检查", "需要生成", "需要分析"]):
                # Strip leading "-", "*", bullets
                clean = re.sub(r"^[-*•·\s]+", "", line).strip()
                if clean:
                    use_cases.append(clean[:200])
                    if len(use_cases) >= 4:
                        break
        return use_cases

    def _extract_params(self, body: str) -> list[dict]:
        """Extract parameter metadata (name, type, required, example) from ## Parameters section.

        Parses both bullet-list format and Markdown table format.
        """
        params: list[dict] = []
        m = re.search(r"(?:^|\n)##\s*(?:parameters?|参数)(.*?)(?=\n##(?!#)|\Z)", body,
                      re.IGNORECASE | re.DOTALL)
        if not m:
            return params
        section = m.group(1)

        # Table format: | name | type | required | ... |
        table_rows = re.findall(
            r"^\s*\|\s*[`']?(\w+)[`']?\s*\|\s*(\w+)\s*\|\s*(\S+)\s*\|([^\n]*)",
            section, re.MULTILINE,
        )
        for row in table_rows:
            name, ptype, required, rest = row
            # Skip header rows
            if name.lower() in {"name", "参数", "参数名", "parameter", "字段", "---"}:
                continue
            example = ""
            # Try to extract example value from description column
            desc_parts = rest.strip().strip("|").strip()
            ex_match = re.search(r"[例如如]?[`'\"]([^`'\"]{3,60})[`'\"]", desc_parts)
            if ex_match:
                example = ex_match.group(1)
            params.append({
                "name": name,
                "type": ptype,
                "required": required.lower() in {"yes", "是", "true", "required", "✓"},
                "example": example,
            })
            if len(params) >= 8:
                break

        if not params:
            # Bullet-list fallback
            for line in section.split("\n"):
                match = re.match(r"[-*]\s+[`\*]?(\w+)[`\*]?\s*[:(—-]", line)
                if match:
                    params.append({"name": match.group(1), "type": "string",
                                   "required": True, "example": ""})
                    if len(params) >= 6:
                        break
        return params

    def _extract_description(self, body: str) -> str:
        """Extract first meaningful line from ## Description section."""
        m = re.search(r"(?:^|\n)##\s*(?:description|概述|简介|overview)(.*?)(?=\n##(?!#)|\Z)",
                      body, re.IGNORECASE | re.DOTALL)
        if not m:
            return ""
        for line in m.group(1).split("\n"):
            line = line.strip()
            if len(line) > 20 and not line.startswith("#"):
                return line[:200]
        return ""

    # ── example values for common parameter names ──────────────────────────────
    _PARAM_EXAMPLE_MAP: dict[str, str] = {
        "skill_path": "/Users/me/.cursor/skills/my-skill",
        "path": "/Users/me/project",
        "url": "https://github.com/example/repo",
        "query": "如何生成测试报告",
        "text": "这是一段示例文本，请分析",
        "content": "# 示例内容\n\n这是测试文档",
        "message": "请帮我完成这个任务",
        "prompt": "生成一份详细的测试计划",
        "input": "示例输入数据",
        "file": "/path/to/file.md",
        "repo": "https://github.com/example/repo",
        "target": "/path/to/target",
        "name": "my-component",
        "title": "测试标题",
        "description": "这是示例描述内容",
        "topic": "AI技术发展趋势",
        "lang": "python",
        "language": "Chinese",
        "format": "markdown",
        "output": "/path/to/output",
        "output_dir": "./output",
    }

    def _build_core_scenarios(self, skill_name: str, params: list[dict],
                              description: str) -> list[tuple[str, str, list]]:
        """Build realistic P0 prompts from skill description and parameter metadata.

        Returns list of (prompt, expected, rubric) tuples.
        """
        scenarios: list[tuple[str, str, list]] = []
        required = [p for p in params if p.get("required")]
        all_params = params[:4]

        # Scenario 1: describe what to do using description + first required param
        if required:
            first = required[0]
            p_name = first["name"]
            p_ex = first.get("example") or self._PARAM_EXAMPLE_MAP.get(p_name, f"<{p_name}>")
            if description:
                prompt = f"请使用 {skill_name} 完成以下任务：{description[:100]}\n参数 {p_name} = {p_ex}"
            else:
                prompt = f"调用 {skill_name}，传入 {p_name}={p_ex}，完整执行并返回结果"
            scenarios.append((prompt, f"{skill_name} 识别 {p_name} 参数并完成核心功能，输出有意义的结果", []))

        # Scenario 2: "典型触发语句 + 真实参数值"
        if all_params:
            parts = []
            for p in all_params[:2]:
                p_name = p["name"]
                p_ex = p.get("example") or self._PARAM_EXAMPLE_MAP.get(p_name, f"<{p_name}>")
                parts.append(f"{p_name}: {p_ex}")
            if description:
                short_desc = description[:80]
                prompt = f"{short_desc}\n" + "\n".join(parts)
            else:
                prompt = f"使用 {skill_name}\n" + "\n".join(parts)
            scenarios.append((prompt, f"{skill_name} 接收参数后完成预期功能", []))

        # Scenario 3: 完全自然语言触发（模仿真实用户输入）
        if description:
            prompt = f"{description[:120]}"
            scenarios.append((prompt, f"{skill_name} 识别用户意图并执行核心功能", []))

        # Final fallback: generic but still better than "基础功能场景 A"
        scenarios.append((
            f"请演示 {skill_name} 的完整使用方式，包括核心功能的典型用例",
            f"{skill_name} 输出完整、有意义的结果，覆盖核心功能点",
            [],
        ))
        return scenarios

    def _build_trigger_edge_scenarios(
        self, skill_name: str, triggers: list[str], description: str
    ) -> list[str]:
        """Edge-case prompts for no_code / behavior skills using their trigger phrases."""
        del skill_name, description
        if not triggers:
            return []
        t = triggers[0]
        return [
            f"{t}，但任务描述非常模糊，只说「帮我做好」，无任何具体要求",
            f"{t}，同时任务极其复杂：同时实现支付系统、推荐引擎和IM聊天功能",
            f"{t}，但当前上下文中没有任何未完成的任务",
            f"连续三次说：{t}，{t}，{t} — 测试重复触发行为是否一致",
            f"{t}，要求用中文和英文分别输出同一份结果",
        ]

    def _build_trigger_error_scenarios(
        self, skill_name: str, triggers: list[str], description: str
    ) -> list[str]:
        """Negative/error-case prompts for no_code / behavior skills."""
        del skill_name, description
        if not triggers:
            return []
        t = triggers[0]
        return [
            t + '，但紧接着说「算了，不用管了」——测试是否能优雅取消',
            f"触发词拼写错误：{t[:3]}xxx，验证是否有容错",
            t + "，然后提供完全矛盾的要求：既要极速完成又要完美无缺且不能用任何工具",
            t + "，但 prompt 里没有任何实际任务内容，只有触发词本身",
        ]

    def _build_edge_scenarios(self, skill_name: str, params: list[str],
                              description: str) -> list[str]:
        """Build edge-case scenario prompts with skill-specific context."""
        desc_hint = f"（{description[:60]}）" if description else ""
        scenarios = [
            f"使用 {skill_name}{desc_hint}，输入极长文本（超过1万字）会怎样？请验证是否有长度限制保护",
            f"使用 {skill_name}，输入包含特殊字符 `<>&\"'\\n\\t` 及 emoji 🎉 时的处理",
            f"使用 {skill_name}，输入内容为空字符串或 null 时应如何响应",
            f"使用 {skill_name}，同时传入最小有效参数（仅必填字段），验证可选参数默认行为",
            f"使用 {skill_name}，在同一会话中连续调用 3 次，验证幂等性或状态一致性",
            f"使用 {skill_name}，输入混合中英文及代码片段，验证多语言处理能力",
        ]
        if params:
            scenarios.append(f"使用 {skill_name}，将参数 `{params[0]}` 设置为最大/最小合法值，观察边界行为")
        return scenarios

    def _build_error_scenarios(self, skill_name: str, params: list[str],
                               description: str) -> list[str]:
        """Build negative/error-case scenario prompts."""
        del description
        scenarios = [
            f"使用 {skill_name}，传入类型错误的参数（期望字符串但传入数字），验证错误处理",
            f"使用 {skill_name}，在缺少所有必填参数的情况下调用，观察是否有明确的错误提示",
            f"使用 {skill_name}，传入格式完全错误的数据（随机乱码），验证是否能安全降级",
            f"使用 {skill_name}，模拟外部依赖不可用（如 API 超时）时的降级行为",
        ]
        if params:
            scenarios.append(f"使用 {skill_name}，将参数 `{params[0]}` 设置为 null/undefined，验证参数校验逻辑")
        return scenarios

    def _make_tc(self, idx: int, priority: str, prompt: str, expected: str,
                 has_code: bool, skill_name: str, extra_rubric: list | None = None) -> dict:
        del skill_name
        tc_id = f"tc_{idx:03d}"
        robustness_checks = (
            [{"description": "执行无异常抛出", "check_type": "no_exception"},
             {"description": "返回结果非空", "check_type": "not_empty"}]
            if has_code else
            [{"description": "SKILL.md 覆盖该用例场景", "check_type": "doc_coverage"},
             {"description": "参数说明完整", "check_type": "param_valid"},
             {"description": "逻辑描述自洽", "check_type": "logic_coherent"}]
        )
        needs_delta = self.weights.delta_max > 0
        baseline = (
            f"不使用任何 skill 或工具，仅凭通用 LLM 能力完成以下任务（对照组）：{prompt}"
            if needs_delta else None
        )
        # v5: profile-differentiated correctness rubric
        rubric = self._build_correctness_rubric(expected, extra_rubric)
        return {
            "id": tc_id,
            "priority": priority,
            "source": "auto",
            "prompt": prompt,
            "expected_behavior": expected,
            "context": {},
            "robustness_checks": robustness_checks,
            "correctness_rubric": rubric,
            "baseline_prompt": baseline,
        }

    def _build_correctness_rubric(self, expected: str, extra_rubric: list | None) -> list[dict]:
        """v5 §4.2: profile-differentiated correctness rubric."""
        if self.profile in ("deterministic", "workflow"):
            rubric = [
                {"criterion": "输出包含必需字段（字段名完全匹配）",
                 "weight": 3.0,
                 "score_levels": {"完全匹配": 1.0, "缺失1个字段": 0.5, "缺失2+字段": 0.0}},
                {"criterion": "字段类型符合预期（string/number/boolean/array）",
                 "weight": 2.0,
                 "score_levels": {"全部符合": 1.0, "部分类型错误": 0.3, "全部类型错误": 0.0}},
                {"criterion": f"业务逻辑正确（符合「{expected[:60]}」的预期）",
                 "weight": 2.0,
                 "score_levels": {"逻辑正确": 1.0, "边界错误": 0.5, "逻辑混乱": 0.0}},
                {"criterion": "无冗余字段（输出精简）",
                 "weight": 1.0,
                 "score_levels": {"无冗余": 1.0, "有无关字段": 0.0}},
            ]
        elif self.profile == "generative":
            rubric = [
                {"criterion": "内容符合 prompt 要求（主题/格式/长度）",
                 "weight": 4.0,
                 "score_levels": {"完全符合": 1.0, "部分偏离": 0.5, "完全跑题": 0.0}},
                {"criterion": "格式规范（如 JSON/代码语法正确）",
                 "weight": 3.0,
                 "score_levels": {"完全规范": 1.0, "有小瑕疵": 0.6, "格式错误": 0.0}},
                {"criterion": "内容质量（流畅度/专业性/无幻觉）",
                 "weight": 3.0,
                 "score_levels": {"高质量": 1.0, "中等（有小问题）": 0.5, "低质量（明显幻觉）": 0.0}},
                {"criterion": "完整性（覆盖 prompt 所有要求点）",
                 "weight": 2.0,
                 "score_levels": {"完整覆盖": 1.0, "遗漏1点": 0.5, "遗漏2+点": 0.0}},
            ]
        elif self.profile == "no_code":
            rubric = [
                {"criterion": "trigger_coverage: SKILL.md 明确列出触发该 skill 的关键词或条件",
                 "weight": 3.0,
                 "score_levels": {"触发条件清晰": 1.0, "触发条件模糊": 0.5, "无触发说明": 0.0}},
                {"criterion": "behavior_definition: 触发后 AI 应表现的行为、风格、约束有具体清晰的描述",
                 "weight": 4.0,
                 "score_levels": {"行为描述详尽": 1.0, "行为描述笼统": 0.5, "无行为说明": 0.0}},
                {"criterion": "constraint_clarity: 红线、安全限制、禁止行为等约束条件有明确说明",
                 "weight": 2.0,
                 "score_levels": {"约束清晰": 1.0, "约束模糊": 0.5, "无约束说明": 0.0}},
                {"criterion": "style_completeness: 风格示例、语言模板或输出规范有具体内容（非占位符）",
                 "weight": 3.0,
                 "score_levels": {"风格完整": 1.0, "风格不完整": 0.5, "无风格说明": 0.0}},
            ]
        else:
            # Default generic rubric
            rubric = [
                {"criterion": f"输出符合「{expected[:80]}」的预期行为", "weight": 2.0,
                 "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}},
                {"criterion": "输出结构完整，无明显遗漏或截断", "weight": 1.5,
                 "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}},
                {"criterion": "语义准确，没有明显错误或误导性内容", "weight": 1.0,
                 "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}},
                {"criterion": "响应格式正确（如 JSON/Markdown/文本 按场景要求）", "weight": 0.5,
                 "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}},
            ]
        if extra_rubric:
            rubric.extend(extra_rubric)
        return rubric

    # Code language tags that indicate the block is a command/snippet, NOT a user prompt
    _CODE_LANG_TAGS = frozenset({
        "bash", "shell", "sh", "zsh", "fish",
        "python", "py", "javascript", "js", "typescript", "ts",
        "ruby", "rb", "go", "rust", "java", "kotlin", "swift",
        "powershell", "ps1", "cmd", "batch", "sql",
        "yaml", "yml", "json", "toml", "xml", "html", "css",
    })

    def _extract_examples(self, body: str) -> list[dict]:
        """Extract input/output pairs from ## Examples section.

        Only picks up untagged (plain) code blocks containing natural language
        prompts; skips bash/python/CLI snippets and markdown table blocks.
        """
        examples = []
        example_section = re.search(
            r"(?:^|\n)##\s*(?:examples?|示例|使用示例)(.*?)(?=\n##(?!#)|\Z)", body,
            re.IGNORECASE | re.DOTALL,
        )
        if not example_section:
            return examples

        section = example_section.group(1)
        # Capture language tag separately so we can filter code blocks
        code_blocks = re.findall(r"```(\w*)\n?(.*?)```", section, re.DOTALL)
        for lang, block in code_blocks[:8]:
            lang = lang.strip().lower()
            block = block.strip()
            if len(block) < 10:
                continue
            # Skip language-tagged code/CLI blocks — they're commands, not prompts
            if lang in self._CODE_LANG_TAGS:
                continue
            # Skip blocks that look like markdown tables (many pipe chars)
            if block.count("|") > 3:
                continue
            # Skip blocks that look like structured commands (long args, backslashes)
            if "\\" in block and block.count("\n") > 1:
                continue
            examples.append({"prompt": block[:300], "expected": "按示例预期返回结果"})
        return examples

    def _build_scoring_criteria(self, eval_id: str, test_cases: list[dict]) -> dict:
        """Build scoring_criteria.json from live ScoreProfile (never hand-fill)."""
        snapshot = self.weights.as_snapshot()
        criteria_by_tc = []

        for tc in test_cases:
            tc_id = tc["id"]
            tc_snapshot = {
                "robust_max": snapshot["robust_max"],
                "correct_max": snapshot["correct_max"],
                "delta_max": snapshot["delta_max"],
            }

            robustness_scoring = []
            for i, rc in enumerate(tc["robustness_checks"]):
                robustness_scoring.append({
                    "check_id": f"r_{i+1:03d}",
                    "description": rc["description"],
                    "check_type": rc["check_type"],
                    "pass_score": 1.0,
                    "fail_score": 0.0,
                    "weight": 1.0,
                })

            correctness_scoring = []
            for i, cr in enumerate(tc["correctness_rubric"]):
                correctness_scoring.append({
                    "assertion_id": f"c_{i+1:03d}",
                    "criterion": cr["criterion"],
                    "weight": cr["weight"],
                    "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0},
                    "scoring_guidance": (
                        f"评估输出是否满足：「{cr['criterion']}」。"
                        f"完全满足（1.0）：输出清晰准确地满足该标准；"
                        f"部分满足（0.5）：输出部分满足但有明显遗漏；"
                        f"不满足（0.0）：输出完全不符合该标准。"
                    ),
                })

            needs_delta = snapshot["delta_max"] > 0
            delta_scoring = {
                "delta_max": snapshot["delta_max"],
                "formula": f"max(0, with_correct - without_correct + 0.5) × {snapshot['delta_max']}",
                "guidance": "对比有无 skill 时的正确性得分差值，持平得 50% delta 分",
            } if needs_delta else None

            criteria_by_tc.append({
                "tc_id": tc_id,
                "weight_snapshot": tc_snapshot,
                "robustness_scoring": robustness_scoring,
                "correctness_scoring": correctness_scoring,
                "delta_scoring": delta_scoring,
            })

        return {
            "eval_id": eval_id,
            "skill_name": self.skill_info.metadata.name,
            "eval_profile": self.profile,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "profile_weight_snapshot": snapshot,
            "criteria_by_tc": criteria_by_tc,
        }
