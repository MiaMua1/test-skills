"""Main evaluation pipeline — orchestrates all layers."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

from evaluator.config import (
    CODE_EXTENSIONS,
    EXCLUDED_DIRS,
    SCORE_PROFILES,
    TYPE_INFERENCE_KEYWORDS,
    TYPE_TO_PROFILE,
    calculate_grade,
    get_score_profiles,
    get_settings,
)
from evaluator.judge.llm_judge import LLMJudge
from evaluator.layers.layer1_screening import Layer1Screening
from evaluator.layers.layer2_static import Layer2Static
from evaluator.layers.layer3_testgen import Layer3TestGen
from evaluator.layers.layer4_dynamic import Layer4Dynamic
from evaluator.layers.layer5_report import Layer5Report
from evaluator.layers.layer6_aggregate import Layer6Aggregate
from evaluator.models.exceptions import BlockedError, EvaluationError
from evaluator.models.skill import EvalProfile, SkillInfo, SkillMetadata, SkillType

logger = structlog.get_logger()


class EvaluationPipeline:
    """Orchestrates the five-layer + aggregate evaluation pipeline."""

    def __init__(
        self,
        skill_path: str | Path,
        mode: str = "full",
        env_type: str = "auto",
        evals_file: str | Path | None = None,
        criteria_file: str | Path | None = None,
        judge_model: str | None = None,
        eval_model: str | None = None,
        with_baseline: bool = False,
        max_cases: int | None = None,
    ) -> None:
        self.skill_path = Path(skill_path).resolve()
        self.mode = mode
        self.env_type = env_type
        self.with_baseline = with_baseline
        self.settings = get_settings()
        self.storage_base = Path(self.settings.storage_base_dir).resolve()
        # model overrides are per-run; LLMJudge exposes both as properties
        self.judge = LLMJudge(model=judge_model, eval_model=eval_model)
        self.log = logger.bind(skill=self.skill_path.name, mode=mode,
                               judge_model=self.judge._judge_model,  # pylint: disable=protected-access
                               eval_model=self.judge.eval_model,
                               with_baseline=with_baseline)
        self.evals_file = Path(evals_file).resolve() if evals_file else None
        self.criteria_file = Path(criteria_file).resolve() if criteria_file else None
        # Baseline comparison is OFF by default. Enabled when:
        #   1. User explicitly passes --with-baseline, AND
        #   2. User did NOT provide both evals+criteria (which implies custom scoring).
        self.skip_baseline = not with_baseline or bool(self.evals_file and self.criteria_file)
        self.max_cases = max_cases

        self._tmpdir = None
        if str(skill_path).startswith("https://github.com/"):
            self.skill_path = self._clone_repo(str(skill_path))
        elif not self.skill_path.exists():
            raise EvaluationError(f"skill_path does not exist: {self.skill_path}")

    def _clone_repo(self, url: str) -> Path:
        import subprocess  # pylint: disable=import-outside-toplevel
        import tempfile  # pylint: disable=import-outside-toplevel
        self._tmpdir_obj = tempfile.TemporaryDirectory(prefix="skill_eval_")
        tmpdir = self._tmpdir_obj.name
        self._tmpdir = tmpdir
        repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
        local = Path(tmpdir) / repo_name
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(local)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0:
            raise EvaluationError(f"git clone failed: {result.stderr}")
        return local

    def __del__(self) -> None:
        if hasattr(self, "_tmpdir_obj") and self._tmpdir_obj:
            self._tmpdir_obj.cleanup()

    # ── public API ────────────────────────────────────────────────────────────

    async def evaluate(self) -> dict:
        """Run the full evaluation pipeline.

        Returns:
            Structured result dict with scores, grade, report paths.
        """
        eval_id = self._make_eval_id()
        skill_info = self._build_skill_info()

        self.log.info("pipeline.start", eval_id=eval_id, profile=skill_info.eval_profile.value)

        layer_results: dict = {}
        blocked_at: int | None = None
        blocked_reason: str = ""

        # Layer 1
        try:
            l1 = Layer1Screening(skill_info)
            layer_results["layer1"] = l1.run()
        except BlockedError as exc:
            blocked_at, blocked_reason = exc.layer, exc.reason
            layer_results["layer1"] = {"score": exc.score, "max_score": SCORE_PROFILES[skill_info.eval_profile.value].layer1_max, "passed": False, "blocking_reason": exc.reason}
            return self._blocked_result(eval_id, skill_info, blocked_at, blocked_reason, layer_results)

        if self.mode == "quick":
            return await self._quick_result(eval_id, skill_info, layer_results)

        # Layer 2
        try:
            l2 = Layer2Static(skill_info)
            layer_results["layer2"] = await l2.run()
        except BlockedError as _exc:
            blocked_at, blocked_reason = _exc.layer, _exc.reason
            layer_results["layer2"] = {"combined_score": 0, "passed": False, "blocking_reason": _exc.reason}
            return self._blocked_result(eval_id, skill_info, blocked_at, blocked_reason, layer_results)

        # Layer 3
        l3 = Layer3TestGen(skill_info, self.storage_base,
                           evals_file=self.evals_file,
                           criteria_file=self.criteria_file,
                           skip_baseline=self.skip_baseline,
                           with_baseline=self.with_baseline,
                           max_cases=self.max_cases)
        layer_results["layer3"] = await l3.run(eval_id)

        # Layer 4
        try:
            l4 = Layer4Dynamic(skill_info, self.storage_base, self.judge,
                               skip_baseline=self.skip_baseline,
                               with_baseline=self.with_baseline)
            layer_results["layer4"] = await l4.run(eval_id)
        except BlockedError as _exc:
            blocked_at, blocked_reason = _exc.layer, _exc.reason
            layer_results["layer4"] = {"status": "blocked", "blocking_reason": _exc.reason}
            # Still generate L5 report so user gets a visible HTML report with the error
            try:
                l5 = Layer5Report(skill_info, self.storage_base)
                report_result = await l5.run(eval_id, layer_results)
                result = self._blocked_result(eval_id, skill_info, blocked_at, blocked_reason, layer_results)
                result["report_path"] = report_result.get("report_path")
                return result
            except Exception:  # pylint: disable=broad-except
                return self._blocked_result(eval_id, skill_info, blocked_at, blocked_reason, layer_results)

        # Layer 5
        l5 = Layer5Report(skill_info, self.storage_base)
        report_result = await l5.run(eval_id, layer_results)

        return self._final_result(eval_id, skill_info, layer_results, report_result)

    async def aggregate(self, eval_ids: list[str]) -> dict:
        """Generate an aggregate report for multiple eval_ids."""
        agg = Layer6Aggregate(self.storage_base)
        return await agg.run(eval_ids)

    async def resume(self, eval_id: str, *, retry_failed: bool = False) -> dict:
        """Resume an interrupted evaluation from L4 checkpoint.

        Skips L1-L3. Loads existing evals.json and scoring_criteria.json.
        Scans results/ directory to find completed test case IDs.
        Re-runs L4 with skip_tc_ids, then generates L5 report.

        Args:
            eval_id: The evaluation ID to resume.
            retry_failed: If True, also re-run test cases that completed
                but scored correct_raw=0 (e.g. due to undetected API errors).
        """
        skill_info = self._build_skill_info()
        self.log.info("pipeline.resume", eval_id=eval_id,
                      profile=skill_info.eval_profile.value, retry_failed=retry_failed)

        # Verify evals and criteria exist
        evals_dir = self.storage_base / "evals" / skill_info.metadata.name / eval_id
        if not (evals_dir / "evals.json").exists():
            raise EvaluationError(f"Cannot resume: evals.json not found for {eval_id}")
        if not (evals_dir / "scoring_criteria.json").exists():
            raise EvaluationError(f"Cannot resume: scoring_criteria.json not found for {eval_id}")

        # Find already-completed test case IDs from results directory
        completed_tc_ids = self._find_completed_tc_ids(
            skill_info.metadata.name, eval_id, retry_failed=retry_failed
        )
        self.log.info("pipeline.resume.completed_cases", count=len(completed_tc_ids),
                      tc_ids=list(completed_tc_ids))

        # Re-run L1+L2 quickly for layer_results (needed by L5)
        layer_results: dict = {}
        try:
            l1 = Layer1Screening(skill_info)
            layer_results["layer1"] = l1.run()
        except BlockedError as _exc:
            layer_results["layer1"] = {"score": _exc.score, "passed": False}

        try:
            l2 = Layer2Static(skill_info)
            layer_results["layer2"] = await l2.run()
        except BlockedError:
            layer_results["layer2"] = {"combined_score": 0, "passed": False}

        # Run L4 with skip_tc_ids
        try:
            l4 = Layer4Dynamic(skill_info, self.storage_base, self.judge,
                               skip_baseline=self.skip_baseline,
                               with_baseline=self.with_baseline)
            layer_results["layer4"] = await l4.run(eval_id, skip_tc_ids=completed_tc_ids)
        except BlockedError as exc:
            layer_results["layer4"] = {"status": "blocked", "blocking_reason": exc.reason}
            # Still generate L5 report so user gets a visible HTML report with the error
            try:
                l5 = Layer5Report(skill_info, self.storage_base)
                report_result = await l5.run(eval_id, layer_results)
                result = self._blocked_result(eval_id, skill_info, 4, exc.reason, layer_results)
                result["report_path"] = report_result.get("report_path")
                return result
            except Exception:  # pylint: disable=broad-except
                return self._blocked_result(eval_id, skill_info, 4, exc.reason, layer_results)

        # Run L5
        l5 = Layer5Report(skill_info, self.storage_base)
        report_result = await l5.run(eval_id, layer_results)

        return self._final_result(eval_id, skill_info, layer_results, report_result)

    async def regenerate_report(self, eval_id: str) -> dict:
        """Regenerate L5 report from existing eval results.

        Reads existing L4 results from storage, reconstructs layer_results,
        then re-runs L5 report generation only.
        """
        skill_info = self._build_skill_info()
        self.log.info("pipeline.regenerate_report", eval_id=eval_id)

        # Reconstruct layer_results from stored data
        layer_results = await self._reconstruct_layer_results(skill_info, eval_id)

        # Run L5
        l5 = Layer5Report(skill_info, self.storage_base)
        report_result = await l5.run(eval_id, layer_results)

        return self._final_result(eval_id, skill_info, layer_results, report_result)

    def _find_completed_tc_ids(self, skill_name: str, eval_id: str,
                              *, retry_failed: bool = False) -> set[str]:
        """Scan results directory to find test cases that can be skipped.

        Args:
            skill_name: Name of the skill being evaluated.
            eval_id: The evaluation ID.
            retry_failed: If True, exclude cases where correct_raw == 0
                so they get re-run instead of skipped.
        """
        completed = set()
        results_dir = self.storage_base / "results" / skill_name / eval_id / "with_skill"
        if results_dir.exists():
            for snapshot_file in results_dir.glob("*.json"):
                try:
                    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
                    status = data.get("status", "")
                    if status != "success":
                        continue
                    # When retry_failed is set, skip only cases that actually scored > 0
                    if retry_failed:
                        correct_raw = data.get("scores", {}).get("correct_raw", 0.0)
                        if correct_raw <= 0:
                            self.log.info("pipeline.resume.retry_zero_score",
                                          tc_id=snapshot_file.stem, correct_raw=correct_raw)
                            continue
                    completed.add(snapshot_file.stem)
                except (json.JSONDecodeError, OSError):
                    pass
        return completed

    async def _reconstruct_layer_results(self, skill_info: SkillInfo, eval_id: str) -> dict:
        """Reconstruct layer_results dict from stored eval_data.json or raw results."""
        layer_results: dict = {}

        # Try loading from existing eval_data.json first
        eval_data_path = (self.storage_base / "reports" / skill_info.metadata.name
                          / eval_id / "eval_data.json")
        if eval_data_path.exists():
            try:
                eval_data = json.loads(eval_data_path.read_text(encoding="utf-8"))
                # eval_data stores layer data under "layers" (not "layer_results")
                layer_results = eval_data.get("layer_results") or eval_data.get("layers") or {}
            except (json.JSONDecodeError, OSError):
                pass

        # If L1 data is missing or empty, re-run L1 screening live
        l1_data = layer_results.get("layer1", {})
        if not l1_data or not l1_data.get("checks"):
            try:
                l1 = Layer1Screening(skill_info)
                layer_results["layer1"] = l1.run()
            except BlockedError as exc:
                layer_results["layer1"] = {"score": exc.score, "passed": False}

        # If L2 data is missing or empty, re-run L2 static analysis live
        l2_data = layer_results.get("layer2", {})
        if not l2_data or (not l2_data.get("code_quality") and not l2_data.get("security") and not l2_data.get("skipped")):
            try:
                l2 = Layer2Static(skill_info)
                layer_results["layer2"] = await l2.run()
            except BlockedError:
                layer_results["layer2"] = {"combined_score": 0, "passed": False}

        # Ensure L4 has per_case data from snapshots
        l4_data = layer_results.get("layer4", {})
        if not l4_data.get("per_case"):
            layer_results["layer4"] = self._load_l4_results_from_snapshots(
                skill_info.metadata.name, eval_id
            )

        return layer_results

    def _load_l4_results_from_snapshots(self, skill_name: str, eval_id: str) -> dict:
        """Load L4 results from individual snapshot files."""
        per_case = []
        results_base = self.storage_base / "results" / skill_name / eval_id
        with_dir = results_base / "with_skill"
        without_dir = results_base / "without_skill"

        if not with_dir.exists():
            return {"status": "skipped", "reason": "No with_skill results found"}

        for snapshot_file in sorted(with_dir.glob("*.json")):
            try:
                with_data = json.loads(snapshot_file.read_text(encoding="utf-8"))
                tc_id = snapshot_file.stem
                without_data = None
                without_file = without_dir / f"{tc_id}.json"
                if without_file.exists():
                    without_data = json.loads(without_file.read_text(encoding="utf-8"))
                per_case.append({
                    "tc_id": tc_id,
                    "priority": with_data.get("input", {}).get("context", {}).get("priority", "P1"),
                    "with": with_data,
                    "without": without_data,
                })
            except (json.JSONDecodeError, OSError):
                continue

        if not per_case:
            return {"status": "skipped", "reason": "No valid result snapshots"}

        # Recompute aggregate scores
        profile = get_score_profiles(self.with_baseline).get(self._build_skill_info().eval_profile.value)
        robust_scores = [c["with"].get("scores", {}).get("robust_raw", 0) for c in per_case]
        correct_with = [c["with"].get("scores", {}).get("correct_raw", 0) for c in per_case]
        correct_without = [c["without"].get("scores", {}).get("correct_raw", 0) if c["without"] else 0 for c in per_case]

        avg_robust = sum(robust_scores) / len(robust_scores) if robust_scores else 0.0
        avg_correct_with = sum(correct_with) / len(correct_with) if correct_with else 0.0
        avg_correct_without = sum(correct_without) / len(correct_without) if correct_without else 0.0

        robust_score = round(avg_robust * profile.robust_max, 2)
        correct_score = round(avg_correct_with * profile.correct_max, 2)

        needs_delta = profile.delta_max > 0 and not self.skip_baseline
        if needs_delta:
            delta_raw = avg_correct_with - avg_correct_without
            delta_normalized = max(0.0, delta_raw + 0.5)
            delta_score = round(delta_normalized * profile.delta_max, 2)
        else:
            delta_raw = delta_normalized = delta_score = 0.0

        return {
            "status": "completed",
            "layer": 4,
            "robust_score": robust_score,
            "robust_max": profile.robust_max,
            "correct_score": correct_score,
            "correct_max": profile.correct_max,
            "delta_score": delta_score,
            "delta_max": profile.delta_max,
            "delta_raw": round(delta_raw, 4),
            "delta_normalized": round(delta_normalized, 4),
            "with_correct": round(avg_correct_with, 4),
            "without_correct": round(avg_correct_without, 4),
            "total_score": round(robust_score + correct_score + delta_score, 2),
            "per_case": per_case,
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _make_eval_id(self) -> str:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        uid = str(uuid.uuid4())[:8]
        return f"{self.skill_path.name}-{ts}-{uid}"

    def _build_skill_info(self) -> SkillInfo:
        """Parse metadata and infer eval_profile from skill directory."""
        metadata, type_inferred = self._parse_metadata()
        has_code = self._detect_has_code()
        eval_profile, type_inferred = self._infer_profile(metadata, has_code, type_inferred)
        return SkillInfo(
            metadata=metadata,
            skill_path=self.skill_path,
            has_code=has_code,
            eval_profile=eval_profile,
            type_inferred=type_inferred,
        )

    def _parse_metadata(self) -> tuple[SkillMetadata, bool]:
        type_inferred = True

        # Try skill.json first
        skill_json_path = self.skill_path / "skill.json"
        if skill_json_path.exists():
            try:
                data = json.loads(skill_json_path.read_text(encoding="utf-8"))
                type_inferred = "type" not in data
                return SkillMetadata(
                    name=data.get("name", self.skill_path.name),
                    version=data.get("version"),
                    type=SkillType(data["type"]) if data.get("type") in SkillType.__members__.values() else None,
                    description=data.get("description", ""),
                    author=data.get("author"),
                    tags=data.get("tags", []),
                    dependencies=data.get("dependencies", []),
                ), type_inferred
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        # Fallback to SKILL.md frontmatter
        skill_md = self.skill_path / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
            if m:
                try:
                    fm = yaml.safe_load(m.group(1)) or {}
                    type_inferred = "type" not in fm
                    skill_type = None
                    if fm.get("type") in (v.value for v in SkillType):
                        skill_type = SkillType(fm["type"])
                    return SkillMetadata(
                        name=fm.get("name", self.skill_path.name),
                        version=fm.get("version"),
                        type=skill_type,
                        description=fm.get("description", ""),
                        author=fm.get("author"),
                    ), type_inferred
                except (yaml.YAMLError, ValueError):
                    pass

        return SkillMetadata(name=self.skill_path.name, description="No description"), True

    def _detect_has_code(self) -> bool:
        for f in self.skill_path.rglob("*"):
            if not f.is_file():
                continue
            rel_parts = set(f.relative_to(self.skill_path).parts)
            if rel_parts & EXCLUDED_DIRS:
                continue
            if f.suffix.lower() in CODE_EXTENSIONS:
                return True
        return False

    def _infer_profile(self, metadata: SkillMetadata, has_code: bool,
                       type_inferred: bool) -> tuple[EvalProfile, bool]:
        # Priority 1: explicit type field
        if metadata.type and not type_inferred:
            profile_str = TYPE_TO_PROFILE.get(metadata.type.value, "deterministic")
            if not has_code:
                return EvalProfile.NO_CODE, False
            return EvalProfile(profile_str), False

        # Priority 2: no code files
        if not has_code:
            return EvalProfile.NO_CODE, type_inferred

        # Keyword-based inference
        desc_lower = (metadata.description or "").lower()
        skill_md = self.skill_path / "SKILL.md"
        if skill_md.exists():
            desc_lower += " " + skill_md.read_text(encoding="utf-8")[:1000].lower()

        for profile_name, keywords in TYPE_INFERENCE_KEYWORDS.items():
            if any(kw in desc_lower for kw in keywords):
                profile_map = {"workflow": EvalProfile.WORKFLOW,
                               "generative": EvalProfile.GENERATIVE,
                               "analyzer": EvalProfile.DETERMINISTIC}
                return profile_map[profile_name], type_inferred

        return EvalProfile.DETERMINISTIC, type_inferred

    def _blocked_result(self, eval_id: str, skill_info: SkillInfo,
                        blocked_at: int, reason: str, layer_results: dict) -> dict:
        total, grade, _ = self._calc_partial(skill_info, layer_results)
        self.log.warning("pipeline.blocked", eval_id=eval_id, layer=blocked_at)
        return {
            "eval_id": eval_id,
            "skill_name": skill_info.metadata.name,
            "eval_profile": skill_info.eval_profile.value,
            "total_score": total,
            "grade": grade,
            "verdict": "BLOCKED",
            "blocked_at": blocked_at,
            "blocking_reason": reason,
            "layer_results": layer_results,
            "report_path": None,
        }

    async def _quick_result(self, eval_id: str, skill_info: SkillInfo,
                            layer_results: dict) -> dict:
        total, grade, verdict = self._calc_partial(skill_info, layer_results)
        return {
            "eval_id": eval_id,
            "skill_name": skill_info.metadata.name,
            "eval_profile": skill_info.eval_profile.value,
            "total_score": total,
            "grade": grade,
            "verdict": verdict,
            "blocked_at": None,
            "mode": "quick",
            "layer_results": layer_results,
            "report_path": None,
        }

    def _final_result(self, eval_id: str, skill_info: SkillInfo,
                      layer_results: dict, report_result: dict) -> dict:
        total = report_result.get("total_score", 0)
        grade = report_result.get("grade", "F")
        _, verdict = calculate_grade(total)
        l4 = layer_results.get("layer4", {})
        self.log.info("pipeline.complete", eval_id=eval_id, score=total, grade=grade)

        # Build baseline hint for profiles that support delta scoring
        baseline_hint = None
        if not self.with_baseline and skill_info.eval_profile.value in ("deterministic", "workflow"):
            baseline_hint = (
                "💡 本次评测未启用基线对比（增值模块）。如需衡量 skill 相对于纯 LLM 的增量价值，"
                "可添加 --with-baseline 参数重新评测，将获得额外最高 30 分的增值评分（独立于基础 100 分）。"
            )

        result = {
            "eval_id": eval_id,
            "skill_name": skill_info.metadata.name,
            "eval_profile": skill_info.eval_profile.value,
            "total_score": total,
            "grade": grade,
            "verdict": verdict,
            "blocked_at": None,
            "layer_scores": {
                "layer1": layer_results.get("layer1", {}).get("score", 0),
                "layer2_quality": layer_results.get("layer2", {}).get("code_quality", {}).get("score", 0),
                "layer2_security": layer_results.get("layer2", {}).get("security", {}).get("score", 0),
                "layer4_robust": l4.get("robust_score", 0),
                "layer4_correct": l4.get("correct_score", 0),
                "layer4_delta": l4.get("delta_score", 0),
            },
            "main_issues": layer_results.get("layer1", {}).get("issues", [])[:3],
            "report_path": report_result.get("report_path"),
            "eval_data_path": report_result.get("eval_data_path"),
        }
        if baseline_hint:
            result["baseline_hint"] = baseline_hint
        return result

    def _calc_partial(self, _skill_info: SkillInfo, layer_results: dict) -> tuple[float, str, str]:
        total = 0.0
        total += layer_results.get("layer1", {}).get("score", 0)
        l2 = layer_results.get("layer2", {})
        total += l2.get("combined_score", 0)
        total += layer_results.get("layer4", {}).get("total_score", 0)
        grade, verdict = calculate_grade(total)
        return round(total, 1), grade, verdict