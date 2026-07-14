"""Layer 5: Report generation — strictly bound to eval_id."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import structlog

from evaluator.config import SCORE_PROFILES, calculate_grade
from evaluator.models.exceptions import ScoreBindingError
from evaluator.models.skill import SkillInfo

logger = structlog.get_logger()


class Layer5Report:
    """Layer 5: Generate eval_data.json and report.html from archived data.

    All max_score values are read from scoring_criteria.json's
    profile_weight_snapshot — never hardcoded.
    """

    layer_number = 5
    layer_name = "layer5_report"

    def __init__(self, skill_info: SkillInfo, storage_base: Path) -> None:
        self.skill_info = skill_info
        self.profile = skill_info.eval_profile.value
        self.storage_base = storage_base
        self._current_eval_data = None
        self.log = logger.bind(layer=self.layer_name, skill=skill_info.metadata.name)

    async def run(self, eval_id: str, layer_results: dict) -> dict:
        """Generate eval_data.json and report.html.

        Args:
            eval_id: Shared evaluation identifier.
            layer_results: Dict with keys layer1, layer2, layer4 results.

        Returns:
            Dict with report_path and eval_data_path.

        Raises:
            ScoreBindingError: On eval_id mismatch or weight sum ≠ 100.
        """
        criteria_path = (self.storage_base / "evals"
                         / self.skill_info.metadata.name / eval_id / "scoring_criteria.json")

        if not criteria_path.exists():
            self.log.warning("layer5.no_criteria", eval_id=eval_id)
            snapshot = SCORE_PROFILES[self.profile].as_snapshot()
        else:
            criteria_data = json.loads(criteria_path.read_text(encoding="utf-8"))

            # Validation 1: eval_id consistency
            if criteria_data.get("eval_id") != eval_id:
                raise ScoreBindingError("eval_id mismatch in scoring_criteria.json", eval_id)

            snapshot = criteria_data.get("profile_weight_snapshot", {})

            # Validation 2: base weight sum (excluding delta bonus) = 100
            base_weight_sum = sum(v for k, v in snapshot.items() if k != "delta_max")
            if abs(base_weight_sum - 100.0) > 0.01:
                raise ScoreBindingError(f"profile_weight_snapshot base sum={base_weight_sum} ≠ 100", eval_id)

        eval_data = self._build_eval_data(eval_id, snapshot, layer_results)

        report_dir = self.storage_base / "reports" / self.skill_info.metadata.name / eval_id
        report_dir.mkdir(parents=True, exist_ok=True)

        eval_data_path = report_dir / "eval_data.json"
        eval_data_path.write_text(json.dumps(eval_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.info("layer5.eval_data_saved", eval_id=eval_id, path=str(eval_data_path))

        # Generate HTML report
        report_path = report_dir / "report.html"
        html = self._render_html(eval_data)
        report_path.write_text(html, encoding="utf-8")
        self.log.info("layer5.report_saved", eval_id=eval_id, path=str(report_path))

        return {
            "eval_data_path": str(eval_data_path),
            "report_path": str(report_path),
            "total_score": eval_data["summary"]["total_score"],
            "grade": eval_data["summary"]["grade"],
        }

    def _build_eval_data(self, eval_id: str, snapshot: dict, layer_results: dict) -> dict:
        l1 = layer_results.get("layer1", {})
        l2 = layer_results.get("layer2", {})
        l4 = layer_results.get("layer4", {})
        # Collect timing metadata from individual layers
        total_duration = sum(
            lr.get("duration_s", 0)
            for lr in [l1, l2, l4]
            if isinstance(lr, dict)
        )
        total_tokens = sum(
            lr.get("token_count", 0)
            for lr in [l1, l2, l4]
            if isinstance(lr, dict)
        )
        for case in l4.get("per_case", []):
            for run in [case.get("with", {}), case.get("without", {})]:
                if isinstance(run, dict):
                    total_duration += run.get("duration_seconds", 0)
                    total_tokens   += run.get("token_count", 0)

        # Assemble score breakdown (max_score always from snapshot)
        score_breakdown = []

        # Determine if code quality is applicable (non-Python / no-code skills get max=0 from layer2)
        quality_max_from_snapshot = snapshot.get("quality_max", 0)
        actual_quality_max = l2.get("code_quality", {}).get("max_score", quality_max_from_snapshot)
        quality_excluded = (actual_quality_max <= 0 < quality_max_from_snapshot)

        # When code quality is excluded, redistribute its weight proportionally
        # to the remaining dimensions so total max stays at 100.
        # Scale factor = 100 / (100 - excluded_weight)
        redistribution_scale = 1.0
        if quality_excluded:
            remaining_base = 100.0 - quality_max_from_snapshot
            redistribution_scale = 100.0 / remaining_base if remaining_base > 0 else 1.0

        # Layer 1: 基础合规 — equal-weight per module (each module = layer1_max / num_modules)
        layer1_max = round(snapshot.get("layer1_max", 15) * redistribution_scale, 2)
        if layer1_max > 0:
            l1_checks = l1.get("checks", {})
            l1_module_keys = [k for k in ["metadata", "documentation", "basic_compliance"] if l1_checks.get(k)]
            num_l1_modules = len(l1_module_keys) or 1
            per_module_max = layer1_max / num_l1_modules
            l1_score = round(sum(
                (l1_checks.get(k, {}).get("score", 0) or 0) / 100 * per_module_max
                for k in l1_module_keys
            ), 2)
            score_breakdown.append({
                "label": "基础合规",
                "score": l1_score,
                "max": layer1_max,
                "layer": 1,
                "source": "scoring_criteria.json → profile_weight_snapshot.layer1_max",
                "source_tc_ids": [],
            })

        # Code quality: only include when actually applicable
        if actual_quality_max > 0:
            score_breakdown.append({
                "label": "代码质量",
                "score": round(l2.get("code_quality", {}).get("score", 0), 2),
                "max": actual_quality_max,
                "layer": 2,
                "source": "scoring_criteria.json → profile_weight_snapshot.quality_max",
                "source_tc_ids": [],
            })

        security_max = round(snapshot.get("security_max", 0) * redistribution_scale, 2)
        score_breakdown.append({
            "label": "安全合规",
            "score": round(l2.get("security", {}).get("score", 0) / snapshot.get("security_max", 1) * security_max, 2) if snapshot.get("security_max", 0) > 0 else 0,
            "max": security_max,
            "layer": 2,
            "source": "scoring_criteria.json → profile_weight_snapshot.security_max",
            "source_tc_ids": [],
        })

        if l4.get("status") == "completed":
            tc_ids = [c["tc_id"] for c in l4.get("per_case", [])]
            robust_max = round(snapshot.get("robust_max", 0) * redistribution_scale, 2)
            correct_max = round(snapshot.get("correct_max", 0) * redistribution_scale, 2)
            score_breakdown.append({
                "label": "健壮性",
                "score": round(l4.get("robust_score", 0) * redistribution_scale, 2),
                "max": robust_max,
                "layer": 4,
                "source": "scoring_criteria.json → profile_weight_snapshot.robust_max",
                "source_tc_ids": tc_ids,
            })
            score_breakdown.append({
                "label": "正确性",
                "score": round(l4.get("correct_score", 0) * redistribution_scale, 2),
                "max": correct_max,
                "layer": 4,
                "source": "scoring_criteria.json → profile_weight_snapshot.correct_max",
                "source_tc_ids": tc_ids,
            })
            delta_max = snapshot.get("delta_max", 0)
            if delta_max > 0:
                score_breakdown.append({
                    "label": "增量价值",
                    "score": l4.get("delta_score", 0),
                    "max": delta_max,
                    "layer": 4,
                    "source": "scoring_criteria.json → profile_weight_snapshot.delta_max",
                    "source_tc_ids": tc_ids,
                })

        total = round(sum(item["score"] for item in score_breakdown), 1)

        # L1 participates in scoring — no longer a pass/fail gate
        grade, verdict = calculate_grade(total)
        status = "completed"
        blocking_reason = None

        # Handle L4 blocked scenarios (e.g. API auth failure)
        l4_blocking = l4.get("blocking_reason")
        if l4.get("status") == "blocked" and l4_blocking:
            status = "blocked"
            blocking_reason = l4_blocking
            verdict = "BLOCKED"


        recommendations = self._build_recommendations(l1, l2, l4, snapshot)
        key_findings = self._build_key_findings(l1, l2, l4, snapshot)

        effect_validation = None
        if l4.get("status") == "completed":
            delta_raw = l4.get("delta_raw", 0)

            # Collect per-case comparison data for baseline analysis
            per_case_comparison = []
            total_with_duration = 0.0
            total_without_duration = 0.0
            total_with_tokens = 0
            total_without_tokens = 0
            for case in l4.get("per_case", []):
                with_run = case.get("with") or {}
                without_run = case.get("without") or {}
                has_without = bool(without_run and (without_run.get("output") or without_run.get("status")))

                w_dur = with_run.get("invoke_duration_s", with_run.get("duration_seconds", 0)) or 0
                wo_dur = (without_run.get("invoke_duration_s", without_run.get("duration_seconds", 0)) or 0) if has_without else 0
                w_tok = with_run.get("token_count", 0) or 0
                wo_tok = (without_run.get("token_count", 0) or 0) if has_without else 0
                w_scores = with_run.get("scores", {}) or {}
                wo_scores = (without_run.get("scores", {}) or {}) if has_without else {}

                total_with_duration += w_dur
                total_without_duration += wo_dur
                total_with_tokens += w_tok
                total_without_tokens += wo_tok

                with_out = with_run.get("output", {}) or {}
                without_out = (without_run.get("output", {}) or {}) if has_without else {}
                with_response = with_out.get("raw_response") or with_out.get("text") or ""
                without_response = without_out.get("raw_response") or without_out.get("text") or ""

                per_case_comparison.append({
                    "tc_id": case.get("tc_id", ""),
                    "priority": case.get("priority", "P1"),
                    "has_without": has_without,
                    "with_duration_s": round(w_dur, 2),
                    "without_duration_s": round(wo_dur, 2),
                    "with_tokens": w_tok,
                    "without_tokens": wo_tok,
                    "with_correct": w_scores.get("correct_raw", 0),
                    "without_correct": wo_scores.get("correct_raw", 0),
                    "with_robust": w_scores.get("robust_raw", 0),
                    "without_robust": wo_scores.get("robust_raw", 0),
                    "with_response": with_response,
                    "without_response": without_response,
                })

            effect_validation = {
                "verdict": "POSITIVE" if delta_raw >= 0 else "NEGATIVE",
                "verdict_text": f"Skill {'具有增量价值' if delta_raw >= 0 else '未超越基线'}",
                "summary": f"with_skill正确率 {l4.get('with_correct', 0):.0%}，without_skill {l4.get('without_correct', 0):.0%}，delta {delta_raw:+.0%}",
                "with_skill_pass_rate": l4.get("with_correct", 0),
                "without_skill_pass_rate": l4.get("without_correct", 0),
                "delta": f"{delta_raw:+.0%}",
                "delta_score": l4.get("delta_score", 0),
                "delta_max": l4.get("delta_max", 0),
                "total_with_duration_s": round(total_with_duration, 2),
                "total_without_duration_s": round(total_without_duration, 2),
                "total_with_tokens": total_with_tokens,
                "total_without_tokens": total_without_tokens,
                "per_case": per_case_comparison,
            }

        return {
            "skill_name": self.skill_info.metadata.name,
            "version": self.skill_info.metadata.version or "",
            "evaluation_date": datetime.now(tz=timezone.utc).isoformat(),
            "eval_id": eval_id,
            "eval_profile": self.profile,
            "meta": {
                "total_duration_s": round(total_duration, 2) if total_duration else None,
                "total_tokens": total_tokens if total_tokens else None,
                "evaluator_version": "2.0.0",
            },
            "summary": {
                "total_score": total,
                "max_score": 100,
                "grade": grade,
                "verdict": verdict,
                "status": status,
                "blocking_reason": blocking_reason,
            },
            "score_breakdown": score_breakdown,
            "layers": {"layer1": l1, "layer2": l2, "layer4": l4},
            "test_cases": [self._build_tc_entry(c) for c in l4.get("per_case", [])],
            "bugs": [],
            "recommendations": recommendations,
            "key_findings": key_findings,
            "effect_validation": effect_validation,
        }

    @staticmethod
    def _build_tc_entry(c: dict) -> dict:
        """Convert a Layer4 per_case entry into the eval-viewer report format."""
        tc_id = c["tc_id"]
        with_run = c.get("with") or {}
        priority = c.get("priority", "P1")
        prompt = with_run.get("input", {}).get("prompt", "")
        output = with_run.get("output", {})
        raw_response = (output.get("raw_response") or output.get("text") or
                        str(output)[:500] if output else "")
        duration_ms = round(with_run.get("duration_seconds", 0) * 1000)
        execution = with_run.get("execution", {}) or {}
        route_label = execution.get("route_label", "未知执行路径")
        failure_tags = execution.get("failure_tags", []) or []
        failure_reason = execution.get("failure_reason", "")
        output_method = output.get("simulation_note", output.get("method", output.get("run_mode", "")))

        # Build assertions from robustness + correctness results
        assertions: list[dict] = []
        if failure_tags:
            assertions.append({
                "id": "exec_failure",
                "description": "执行路径/运行稳定性",
                "passed": False,
                "actual": f"{route_label} · {','.join(failure_tags)} · {failure_reason}",
            })
        for r in with_run.get("robustness_results", []):
            assertions.append({
                "id": r.get("check_id", ""),
                "description": r.get("check_type", r.get("detail", "")),
                "passed": r.get("passed", False),
                "actual": r.get("detail", ""),
            })
        for r in with_run.get("correctness_results", []):
            score = r.get("score", 0)
            assertions.append({
                "id": r.get("assertion_id", ""),
                "description": r.get("criterion", ""),
                "passed": score >= 0.5,
                "actual": r.get("reasoning", r.get("level", "")),
            })

        # Determine result
        if not assertions:
            result = "pending"
        else:
            passed_count = sum(1 for a in assertions if a["passed"])
            ratio = passed_count / len(assertions)
            result = "pass" if ratio >= 0.8 else "partial" if ratio >= 0.4 else "fail"

        return {
            "id": tc_id,
            "label": tc_id,
            "description": tc_id,
            "priority": priority,
            "result": result,
            "prompt": prompt,
            "execution_record": {
                "method": output_method or route_label,
                "input": prompt,
                "output": raw_response,  # full response, no truncation
                "duration_ms": duration_ms,
            },
            "assertions": assertions,
        }

    def _build_recommendations(self, l1: dict, l2: dict, l4: dict, snapshot: dict) -> list[str]:
        recs = []
        # Layer 1 is pass/fail only
        if not l1.get("passed", True):
            for issue in l1.get("issues", [])[:2]:
                recs.append(f"修复 Layer 1 问题：{issue[:80]}")

        if l2:
            sec = l2.get("security", {})
            if sec.get("critical_issues"):
                recs.append("立即修复 CRITICAL 安全漏洞")
            cq = l2.get("code_quality", {})
            quality_max = snapshot.get("quality_max", 0)
            if quality_max > 0 and cq.get("score", 0) < quality_max * 0.8:
                recs.append("提升代码质量：补充类型注解，降低圈复杂度")

        if l4.get("status") == "completed":
            if l4.get("delta_raw", 0) < 0:
                recs.append("Skill 增量价值为负，检查核心逻辑是否比基线 LLM 更有价值")
        return recs[:6]

    def _build_key_findings(self, l1: dict, l2: dict, l4: dict, snapshot: dict) -> dict:
        del snapshot
        strengths, issues = [], []
        # Layer 1 pass/fail
        if l1.get("passed", False):
            strengths.append("文档与基础合规检查通过")

        if l2:
            if not l2.get("security", {}).get("critical_issues"):
                strengths.append("零 CRITICAL 安全漏洞")

        if l4.get("status") == "completed" and l4.get("delta_raw", 0) > 0.2:
            strengths.append(f"显著增量价值（delta={l4.get('delta_raw', 0):+.0%}）")

        issues.extend(l1.get("issues", [])[:3])
        return {"strengths": strengths[:5], "issues": issues[:5]}

    # ── HTML report rendering ─────────────────────────────────────────────────

    _GRADE_COLOR = {"A": "#22c55e", "B": "#3b82f6", "C": "#f59e0b", "D": "#f97316", "F": "#ef4444"}

    @staticmethod
    def _short_path(location: str) -> str:
        """Extract relative file path from absolute location, stripping known parent dirs."""
        import re
        # Strip everything up to and including the skill root directory name
        # e.g. /Users/.../table-analyst-master-abc123/scripts/load_table.py:55 -> scripts/load_table.py:55
        shortened = re.sub(r'^.*/[^/]*-[0-9a-f]{10,}/', '', location)
        if shortened != location:
            return shortened
        # Fallback: strip common path prefixes up to a recognizable project dir
        parts = location.split('/')
        # Find the last segment that looks like a project root (contains '-master-' or similar)
        for idx, part in enumerate(parts):
            if re.search(r'-master-|-main-|-[0-9a-f]{8,}', part):
                return '/'.join(parts[idx + 1:])
        # Final fallback: just show filename:line
        if '/' in location:
            return location.rsplit('/', 1)[-1] if len(location) > 60 else location
        return location

    @staticmethod
    def _esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    @staticmethod
    def _score_color(pct: float) -> str:
        return "#22c55e" if pct >= 80 else "#f59e0b" if pct >= 60 else "#ef4444"

    @staticmethod
    def _fmt_dt(iso: str) -> str:
        if not iso:
            return ""
        try:
            from datetime import timedelta  # pylint: disable=import-outside-toplevel
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            cst = dt + timedelta(hours=8)
            return cst.strftime("%Y-%m-%d %H:%M:%S CST")
        except Exception:  # pylint: disable=broad-except
            return iso[:19]

    def _render_html(self, eval_data: dict) -> str:  # pylint: disable=too-many-locals
        """Generate a single-page card-style HTML report."""
        self._current_eval_data = eval_data
        e = self._esc
        _sc = self._score_color
        d = eval_data
        grade = d["summary"]["grade"]
        score = d["summary"]["total_score"]
        gc = self._GRADE_COLOR.get(grade, "#ef4444")
        verdict = d["summary"].get("verdict", "")
        verdict_badge = (
            '<span class=\'badge fail\'>🚫 已阻断</span>' if verdict == "BLOCKED"
            else '<span class=\'badge pass\'>✅ PASSED</span>' if score >= 60
            else '<span class=\'badge fail\'>❌ FAILED</span>'
        )
        meta = d.get("meta", {})
        total_s = meta.get("total_duration_s") or 0
        tokens = meta.get("total_tokens") or 0
        l1 = d.get("layers", {}).get("layer1", {})
        l2 = d.get("layers", {}).get("layer2", {})
        l4 = d.get("layers", {}).get("layer4", {})

        # Per-layer timing
        l1_s = l1.get("duration_s", 0) or 0
        l2_s = l2.get("duration_s", 0) or 0
        l4_s = l4.get("duration_s", 0) or 0
        timing_detail = f"L1:{l1_s:.2f}s · L2:{l2_s:.2f}s · L4:{l4_s:.2f}s"

        parts = [self._html_head(d), '<div class="wrap">']
        parts.append(self._html_header(d, grade, gc, score, verdict_badge, total_s, tokens, timing_detail))

        # Show blocking reason banner when evaluation was blocked
        blocking_reason = d.get("summary", {}).get("blocking_reason")
        if blocking_reason:
            parts.append(f"""
<div class="card" style="background:#fef2f2;border:2px solid #ef4444;border-radius:12px">
  <div style="display:flex;align-items:flex-start;gap:12px">
    <span style="font-size:28px;flex-shrink:0">🚫</span>
    <div>
      <div style="font-size:16px;font-weight:700;color:#dc2626;margin-bottom:6px">评测已终止</div>
      <div style="color:#991b1b;font-size:14px;line-height:1.6">{self._esc(blocking_reason)}</div>
    </div>
  </div>
</div>""")

        parts.append(self._html_score_table(d))
        parts.append(self._html_layer1(l1))
        parts.append(self._html_layer2(l2))
        parts.append(self._html_layer4(l4))
        parts.append(self._html_baseline_comparison(d))
        parts.append(self._html_findings(d))
        parts.append(f"""
<div style="text-align:center;color:#94a3b8;font-size:12px;margin-top:24px;padding-bottom:24px">
  Skill Evaluator v2.0 · eval_id: {e(d.get('eval_id',''))} · {e(self._fmt_dt(d.get('evaluation_date','')))}
</div>
</div></body></html>""")
        return "\n".join(parts)

    def _html_head(self, d: dict) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Skill 评测报告 — {self._esc(d.get('skill_name',''))}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b;padding:24px}}
.wrap{{max-width:1020px;margin:0 auto}}
.card{{background:white;border-radius:12px;padding:22px 24px;box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:16px}}
.card-title{{font-size:14px;font-weight:700;color:#475569;margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid #e2e8f0;display:flex;align-items:center;gap:8px}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid #f1f5f9;font-size:13px}}
th{{background:#f8fafc;font-weight:600;color:#64748b;font-size:12px}}
tr:last-child td{{border-bottom:none}}
details>summary{{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px}}
details>summary::-webkit-details-marker{{display:none}}
details>summary::before{{content:'▶';font-size:10px;color:#94a3b8;transition:transform .2s}}
details[open]>summary::before{{transform:rotate(90deg)}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}}
.pass{{background:#dcfce7;color:#166534}}.fail{{background:#fee2e2;color:#991b1b}}
.warn{{background:#fef9c3;color:#92400e}}.info{{background:#dbeafe;color:#1e40af}}
.p0{{background:#dc2626;color:white}}.p1{{background:#f97316;color:white}}.p2{{background:#eab308;color:white}}
.sev-critical{{background:#7c3aed;color:white}}.sev-high{{background:#dc2626;color:white}}
.sev-medium{{background:#f97316;color:white}}.sev-low{{background:#eab308;color:white}}
.sev-warning{{background:#94a3b8;color:white}}
.bar-wrap{{background:#e2e8f0;border-radius:5px;height:8px;flex:1}}
.bar-fill{{border-radius:5px;height:8px}}
.score-num{{font-size:13px;font-weight:700;white-space:nowrap}}
.section-layer{{border-left:3px solid {self._GRADE_COLOR.get(d.get('summary',{}).get('grade','F'),'#f59e0b')};padding-left:14px;margin-bottom:12px}}
.tc-card{{border:1px solid #e2e8f0;border-radius:8px;margin-bottom:10px;overflow:hidden}}
.tc-header{{padding:10px 14px;background:#f8fafc;display:flex;align-items:center;gap:10px;cursor:pointer}}
.tc-body{{padding:14px;display:none}}
details.tc[open] .tc-body{{display:block}}
details.tc-card[open] .tc-body{{display:block}}
.compare-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}}
.compare-col{{border-radius:8px;padding:12px;font-size:13px}}
.with-col{{background:#f0fdf4;border:1px solid #bbf7d0}}
.without-col{{background:#fff7ed;border:1px solid #fed7aa}}
.mono{{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;background:#f1f5f9;padding:2px 6px;border-radius:3px}}
.delta-pos{{color:#16a34a;font-weight:700}}.delta-neg{{color:#dc2626;font-weight:700}}.delta-neu{{color:#64748b;font-weight:700}}
pre{{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto;overflow-y:auto;max-height:360px;white-space:pre-wrap;word-break:break-all}}
</style>
</head>
<body>"""

    def _html_header(self, d: dict, grade: str, gc: str, score: float,
                     verdict_badge: str, total_s: float, tokens: int, timing_detail: str) -> str:
        e = self._esc
        token_html = f"<span style='color:#94a3b8;font-size:12px'>🔤 Token {tokens:,}</span>" if tokens else ""
        return f"""
<div class="card" style="display:flex;align-items:center;gap:20px">
  <div style="width:80px;height:80px;border-radius:50%;background:{gc};display:flex;align-items:center;
              justify-content:center;font-size:36px;font-weight:700;color:white;flex-shrink:0">{e(grade)}</div>
  <div style="flex:1">
    <h1 style="font-size:22px;margin-bottom:2px">{e(d.get('skill_name',''))}</h1>
    <div style="color:#64748b;font-size:13px">{e(d.get('version','') or 'vN/A')} · {e(d.get('eval_profile',''))} profile · {e(self._fmt_dt(d.get('evaluation_date','')))}</div>
    <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
      <span style="font-size:34px;font-weight:800;color:{gc}">{score}</span>
      <span style="color:#94a3b8;font-size:16px">/100</span>
      {verdict_badge}
      <span style='color:#94a3b8;font-size:12px'>⏱ 总耗时 {round(total_s,1)}s</span>
      {token_html}
    </div>
    <div style='font-size:11px;color:#94a3b8;margin-top:2px'>{e(timing_detail)}</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:4px;font-family:monospace">eval_id: {e(d.get('eval_id',''))}</div>
  </div>
</div>"""

    def _html_score_table(self, d: dict) -> str:
        e = self._esc
        _sc = self._score_color
        rows = []
        colors = ["#6366f1", "#8b5cf6", "#ec4899", "#f97316", "#22c55e", "#3b82f6", "#06b6d4"]
        for i, item in enumerate(d.get("score_breakdown", [])):
            color = colors[i % len(colors)]
            s = item["score"]
            m = item.get("max", item.get("max_score", 1)) or 1
            pct = round(s / m * 100)
            layer = item.get("layer", 1)
            rows.append(
                f"<tr><td><span class='badge info'>L{layer}</span> {e(item['label'])}</td>"
                f"<td class='score-num' style='color:{color}'>{s}</td>"
                f"<td style='color:#94a3b8'>{m}</td>"
                f"<td style='width:180px'><div style='display:flex;align-items:center;gap:8px'>"
                f"<div class='bar-wrap'><div class='bar-fill' style='background:{color};width:{pct}%'></div></div>"
                f"<span style='font-size:12px;color:#64748b;width:36px'>{pct}%</span></div></td>"
                f"<td style='font-size:11px;color:#94a3b8;max-width:200px;overflow:hidden;text-overflow:ellipsis'>"
                f"{e(str(item.get('source',''))[:60])}</td></tr>"
            )
        return f"""
<div class="card">
  <div class="card-title">📊 评分总览</div>
  <table>
    <tr><th>维度</th><th>得分</th><th>满分</th><th>达成率</th><th>来源</th></tr>
    {''.join(rows)}
  </table>
</div>"""

    def _html_layer1(self, l1: dict) -> str:
        """Render Layer 1 results with score."""
        if not l1:
            return ""
        e = self._esc
        l1_raw_score = l1.get("score", 0) or 0
        # Read layer1_max from score_breakdown to show actual max (e.g. 15), not raw 100
        layer1_max = 15
        for item in (self._current_eval_data or {}).get("score_breakdown", []):
            if item.get("label") == "基础合规":
                layer1_max = item.get("max", 15)
                break
        # Equal-weight: each module = layer1_max / num_modules, sum = L1 total
        check_defs_keys = ["metadata", "documentation", "basic_compliance"]
        num_l1_modules = sum(1 for k in check_defs_keys if l1.get("checks", {}).get(k)) or 1
        per_module_max = layer1_max / num_l1_modules
        l1_weighted_score = round(sum(
            (l1.get("checks", {}).get(k, {}).get("score", 0) or 0) / 100 * per_module_max
            for k in check_defs_keys if l1.get("checks", {}).get(k)
        ), 2)
        score_badge = f"<span style='font-weight:700;font-size:14px;color:{self._score_color(l1_raw_score)}'>{l1_weighted_score}</span><span style='color:#94a3b8;font-size:12px'>/{layer1_max}</span>"
        dur = l1.get("duration_s", 0) or 0
        checks = l1.get("checks", {})
        summary = l1.get("summary", {})

        check_defs = [
            ("metadata",          "🏷️ 元数据", "required"),
            ("documentation",     "📄 文档", "required"),
            ("basic_compliance",  "🔒 合规", "required"),
        ]
        tiles_html = []
        for key, label, _ in check_defs:
            chk = checks.get(key, {})
            if not chk:
                continue
            items = chk.get("items", [])

            # Count required vs optional
            req_passed = sum(1 for i in items if i.get("passed") and not i.get("optional", False))
            req_total = sum(1 for i in items if not i.get("optional", False))
            # Pass if all required items pass
            chk_passed = req_passed == req_total and req_total > 0
            tile_badge = (
                "<span class='badge pass' style='font-size:10px'>通过</span>"
                if chk_passed else "<span class='badge fail' style='font-size:10px'>未通过</span>"
            )
            opt_passed = sum(1 for i in items if i.get("passed") and i.get("optional", False))
            opt_total = sum(1 for i in items if i.get("optional", False))

            # Sort items: required first, optional last
            sorted_items = sorted(items, key=lambda x: (x.get("optional", False), x.get("label", "")))

            item_rows = []
            for it in sorted_items:
                icon = "✅" if it.get("passed") else "❌"
                optional_tag = " <span style='font-size:10px;color:#94a3b8'>(可选)</span>" if it.get("optional", False) else ""
                detail = e(str(it.get("detail", "")))
                label_text = e(it.get("label", ""))
                item_rows.append(
                    f"<tr style='font-size:12px'><td style='padding:4px 8px'>{icon}&nbsp;{label_text}{optional_tag}</td>"
                    f"<td style='padding:4px 8px;color:#475569'>{detail}</td></tr>"
                )
            detail_html = ""
            if item_rows:
                detail_html = f"""
<details open style="margin-top:8px">
  <summary style="font-size:11px;color:#64748b;cursor:pointer;user-select:none;font-weight:600">
    📋 检查明细 ({len(items)} 项，点击折叠/展开)
  </summary>
  <div style="margin-top:6px">
    <table style="width:100%;border-collapse:collapse">
      <tr style="background:#f1f5f9"><th style="padding:4px 8px;text-align:left;font-size:11px">检查项</th>
      <th style="padding:4px 8px;font-size:11px">说明</th></tr>
      {''.join(item_rows)}
    </table>
    {''.join(f"<div style='font-size:11px;color:#dc2626;margin-top:4px'>⚠️ {e(iss)}</div>" for iss in chk.get('issues',[]))}
  </div>
</details>"""
            stats_html = f"""
<div style="font-size:11px;color:#64748b;margin-top:6px">
  必须项：{req_passed}/{req_total} 通过
  {f"· 可选项：{opt_passed}/{opt_total}" if opt_total > 0 else ""}
</div>"""
            # Calculate module weighted score: each module equally shares layer1_max
            module_raw_score = chk.get("score", None)
            num_modules = sum(1 for k2, _, _ in check_defs if checks.get(k2))
            if module_raw_score is not None and num_modules > 0:
                module_max = round(layer1_max / num_modules, 2)
                module_weighted = round(module_raw_score / 100 * module_max, 2)
                module_score_html = (
                    f"<span style='font-size:18px;font-weight:700;color:{self._score_color(module_raw_score)}'>{module_weighted}</span>"
                    f"<span style='font-size:12px;color:#94a3b8'>/{module_max}</span>"
                )
            elif module_raw_score is not None:
                # Fallback: still convert to weighted score for consistency
                module_max_fb = round(layer1_max / max(num_modules, 1), 2)
                module_weighted_fb = round(module_raw_score / 100 * module_max_fb, 2)
                module_score_html = (
                    f"<span style='font-size:18px;font-weight:700;color:{self._score_color(module_raw_score)}'>{module_weighted_fb}</span>"
                    f"<span style='font-size:12px;color:#94a3b8'>/{module_max_fb}</span>"
                )
            else:
                total_items = req_total + opt_total
                passed_items = req_passed + opt_passed
                pct = round(passed_items / max(total_items, 1) * 100)
                module_max_fb = round(layer1_max / max(num_modules, 1), 2)
                module_weighted_fb = round(pct / 100 * module_max_fb, 2)
                module_score_html = (
                    f"<span style='font-size:18px;font-weight:700;color:{self._score_color(pct)}'>{module_weighted_fb}</span>"
                    f"<span style='font-size:12px;color:#94a3b8'>/{module_max_fb}</span>"
                )

            tiles_html.append(f"""
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 14px">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
    <span style="display:flex;align-items:center;gap:6px"><span style="font-weight:600;font-size:13px">{label}</span>{tile_badge}</span>
    <span style="display:flex;align-items:center;gap:6px">{module_score_html}</span>
  </div>
  {stats_html}
  {detail_html}
</div>""")

        # Summary stats
        meta_sum = summary.get("metadata", {})
        doc_sum = summary.get("documentation", {})
        comp_sum = summary.get("basic_compliance", {})
        total_req_passed = (meta_sum.get("required_passed", 0) + doc_sum.get("required_passed", 0) + comp_sum.get("required_passed", 0))
        total_req_total = (meta_sum.get("required_total", 0) + doc_sum.get("required_total", 0) + comp_sum.get("required_total", 0))
        total_opt_passed = (meta_sum.get("optional_passed", 0) + doc_sum.get("optional_passed", 0) + comp_sum.get("optional_passed", 0))
        total_opt_total = (meta_sum.get("optional_total", 0) + doc_sum.get("optional_total", 0) + comp_sum.get("optional_total", 0))

        return f"""
<div class="card">
  <details open>
    <summary class="card-title">
      🔍 Layer 1 — 快速筛查（基础合规）&nbsp;
      {"<span class='badge pass'>✅ 通过</span>" if total_req_passed == total_req_total and total_req_total > 0 else "<span class='badge fail'>❌ 未通过</span>"}
      &nbsp;{score_badge}&nbsp;
      <span style='font-size:13px;color:#64748b'>⏱ {dur:.3f}s</span>
    </summary>
    <div style="margin-top:12px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
      {''.join(tiles_html)}
    </div>
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid #e2e8f0;font-size:12px;color:#64748b">
      📊 汇总：必须项 {total_req_passed}/{total_req_total} 通过
      {f"· 可选项 {total_opt_passed}/{total_opt_total}" if total_opt_total > 0 else ""}
      <span style='color:#94a3b8;margin-left:8px'>（所有必须项通过且无可选项阻挡时总体通过）</span>
    </div>
  </details>
</div>"""

    def _html_layer2(self, l2: dict) -> str:  # pylint: disable=too-many-locals
        if not l2:
            return ""
        e = self._esc
        sc = self._score_color
        l2_passed = l2.get("passed", False)
        l2_status_badge = (
            "<span class='badge pass'>✅ 通过</span>"
            if l2_passed else "<span class='badge fail'>❌ 未通过</span>"
        )
        dur = l2.get("duration_s", 0) or 0
        cq = l2.get("code_quality", {}) or {}
        sec = l2.get("security", {}) or {}
        cq_score = round(cq.get("score", 0), 2)
        cq_max = cq.get("max_score", cq.get("max", 20))
        sec_score = round(sec.get("score", 0), 2)
        sec_max = sec.get("max_score", sec.get("max", 20))
        combined = round(cq_score + sec_score, 2)

        # Code quality detail
        cq_pct = round(cq_score / max(cq_max, 1) * 100)
        cq_col = sc(cq_pct)
        issues = cq.get("issues", []) or []
        issue_rows = []
        for iss in issues:
            if isinstance(iss, dict):
                sev = iss.get("severity", "warning")
                sev_cls = f"sev-{sev}" if sev in ("critical","high","medium","low","warning") else "sev-warning"
                issue_rows.append(
                    f"<tr><td><span class='badge {sev_cls}'>{e(sev.upper())}</span></td>"
                    f"<td>{e(str(iss.get('description', iss))[:100])}</td>"
                    f"<td class='mono'>{e(self._short_path(str(iss.get('location',''))))}</td></tr>"
                )
            else:
                issue_rows.append(f"<tr><td><span class='badge sev-warning'>WARNING</span></td><td colspan='2'>{e(str(iss)[:100])}</td></tr>")
        calc_note = cq.get("formula_note", "")
        cq_summary = cq.get("summary", "")

        # 新增：展示 check_items 代码质量检查项汇总
        check_items_cq = cq.get("check_items", []) or []
        cq_passed = all(ci.get("passed", True) for ci in check_items_cq) if check_items_cq else (cq_score >= cq_max * 0.8)
        cq_badge = (
            "<span class='badge pass' style='font-size:10px'>通过</span>"
            if cq_passed else "<span class='badge fail' style='font-size:10px'>未通过</span>"
        )
        check_items_rows = []
        for ci in check_items_cq:
            icon = "✅" if ci.get("passed", True) else "❌"
            badge_cls = "pass" if ci.get("passed", True) else "fail"
            check_items_rows.append(
                f"<tr><td style='font-size:12px;white-space:nowrap'>{icon} {e(ci.get('label',''))}</td>"
                f"<td style='white-space:nowrap'><span class='badge {badge_cls}' style='font-size:11px'>"
                f"{'通过' if ci.get('passed', True) else '未通过'}</span></td>"
                f"<td style='font-size:12px;color:#64748b'>{e(ci.get('detail',''))}</td></tr>"
            )
        check_items_html = f"""
<div style="margin:8px 0">
  <div style="font-size:11px;font-weight:600;color:#475569;margin-bottom:4px">代码质量检查项汇总</div>
  <table style="width:100%;font-size:12px">
    <tr><th>检查项</th><th>状态</th><th>详情</th></tr>
    {(''.join(check_items_rows)) if check_items_rows else "<tr><td colspan=3 style='color:#94a3b8'>无检查项数据</td></tr>"}
  </table>
</div>""" if check_items_cq else ""

        cq_detail_rows = f"<table style='width:100%;font-size:12px'><tr><th>级别</th><th>问题</th><th>位置</th></tr>{''.join(issue_rows) if issue_rows else '<tr><td colspan=3 style=color:#16a34a>✓ 代码质量良好</td></tr>'}</table>"
        cq_detail = f"""
<details open style="margin-bottom:8px">
  <summary style="font-size:12px;color:#64748b;cursor:pointer;user-select:none">
    ▸ 代码质量详情（E:{cq.get('error_count',0)} W:{cq.get('warning_count',len(issues))}）
  </summary>
  <div style="margin-top:8px">
    {check_items_html}
    {f"<div style='font-family:monospace;font-size:11px;background:#f8fafc;padding:8px;border-radius:4px;margin-bottom:6px'>{e(calc_note)}</div>" if calc_note else ""}
    {f"<div style='font-size:12px;color:#64748b;margin-bottom:8px'>{e(cq_summary)}</div>" if cq_summary else ""}
    <div style="font-size:11px;font-weight:600;color:#475569;margin:8px 0 4px">Pylint/Radon 问题明细</div>
    {cq_detail_rows}
  </div>
</details>"""

        # Security detail
        sec_pct = round(sec_score / max(sec_max, 1) * 100)
        sec_col = sc(sec_pct)
        scans = sec.get("scans", []) or []
        scan_rows = []
        for scan in scans:
            icon = "✅" if scan.get("passed", True) else "❌"
            impact = scan.get("score_impact", scan.get("impact", 0))
            impact_html = f"<span style='color:#16a34a'>+{impact}</span>" if impact >= 0 else f"<span style='color:#dc2626'>{impact}</span>"
            scan_rows.append(
                f"<tr><td style='font-size:12px'>{icon} {e(scan.get('name',''))}</td>"
                f"<td style='text-align:right;font-size:12px;color:#16a34a'>{impact_html}</td>"
                f"<td style='font-size:12px;color:#64748b'>{e(str(scan.get('detail',''))[:60])}</td></tr>"
            )
        crit = sec.get("critical_issues", []) or []
        # Use all_findings (actual regex findings) for detailed issue display
        all_findings = sec.get("all_findings", sec.get("issues", [])) or []
        sec_issue_rows = []
        for iss in all_findings:
            sev = iss.get("severity", "HIGH") if isinstance(iss, dict) else "HIGH"
            sev_lower = sev.lower()
            sev_cls = f"sev-{sev_lower}" if sev_lower in ("critical","high","medium","low","warning") else "sev-high"
            desc = iss.get("description", str(iss)) if isinstance(iss, dict) else str(iss)
            loc = f"{iss.get('file','')}:{iss.get('line','')}" if isinstance(iss, dict) else ""
            snippet = iss.get("snippet", "") if isinstance(iss, dict) else ""
            sec_issue_rows.append(
                f"<tr><td><span class='badge {sev_cls}'>{e(sev.upper())}</span></td>"
                f"<td>{e(str(desc))}</td>"
                f"<td class='mono' style='font-size:11px'>{e(str(loc))}</td>"
                f"<td style='font-size:11px;color:#94a3b8;max-width:200px'>{e(snippet[:60])}</td></tr>"
            )

        # Security category check_items (8 categories: 命令注入, 代码注入, ...)
        check_items_sec = sec.get("check_items", []) or []
        sec_passed = all(ci.get("passed", True) for ci in check_items_sec) if check_items_sec else not bool(crit)
        sec_badge = (
            "<span class='badge pass' style='font-size:10px'>通过</span>"
            if sec_passed else "<span class='badge fail' style='font-size:10px'>未通过</span>"
        )
        cat_rows = []
        for ci in check_items_sec:
            icon = "✅" if ci.get("passed", True) else "❌"
            findings_count = len(ci.get("findings", []))
            detail_text = ci.get("detail", "")
            badge_cls = "pass" if ci.get("passed", True) else "fail"
            cat_rows.append(
                f"<tr><td style='font-size:12px'>{icon} {e(ci.get('label',''))}</td>"
                f"<td><span class='badge {badge_cls}' style='font-size:11px'>"
                f"{'通过' if ci.get('passed', True) else f'{findings_count}处发现'}</span></td>"
                f"<td style='font-size:12px;color:#64748b'>{e(detail_text)}</td></tr>"
            )

        sec_detail = f"""
<details open style="margin-bottom:8px">
  <summary style="font-size:12px;color:#64748b;cursor:pointer;user-select:none;font-weight:600">
    🔎 安全检查详情（{len(check_items_sec)} 类检查项）
  </summary>
  <div style="margin-top:8px">
    {f"<div style='background:#fee2e2;border:1px solid #fca5a5;border-radius:6px;padding:8px;margin-bottom:8px;color:#991b1b;font-size:12px'>🚫 发现 {len(crit)} 个严重安全漏洞，请立即修复</div>" if crit else ""}
    <div style="font-size:11px;font-weight:600;color:#475569;margin-bottom:4px">分类检查结果</div>
    <table style="width:100%;font-size:12px"><tr><th>安全分类</th><th>状态</th><th>说明</th></tr>
    {(''.join(cat_rows)) if cat_rows else "<tr><td colspan=3 style='color:#94a3b8'>无分类数据</td></tr>"}
    </table>
    <div style="font-size:11px;font-weight:600;color:#475569;margin:8px 0 4px">工具扫描摘要（regex / bandit / pip-audit）</div>
    <table style="width:100%;font-size:12px"><tr><th>扫描工具</th><th style='text-align:right'>得分影响</th><th>说明</th></tr>
    {(''.join(scan_rows)) if scan_rows else "<tr><td colspan=3 style='color:#94a3b8'>无扫描数据</td></tr>"}
    </table>
    <div style="font-size:11px;font-weight:600;color:#475569;margin:8px 0 4px">发现的安全问题明细</div>
    <table style="width:100%;font-size:12px"><tr><th>级别</th><th>问题描述</th><th>位置</th><th>代码片段</th></tr>
    {(''.join(sec_issue_rows)) if sec_issue_rows else "<tr><td colspan='4' style='color:#16a34a;text-align:center;font-size:12px'>✓ 无安全问题发现</td></tr>"}
    </table>
  </div>
</details>"""

        skip_note = ""
        if l2.get("skipped"):
            skip_note = f"<div style='font-size:12px;color:#94a3b8;margin-top:8px'>ℹ️ {e(l2.get('reason',''))}</div>"

        return f"""
<div class="card">
  <details open>
    <summary class="card-title">
      ⚙️ Layer 2 — 静态代码分析 &nbsp;{l2_status_badge}&nbsp;<span class='badge info'>tool</span>&nbsp;
      <span style='font-size:13px;color:#64748b'>综合 {combined}/{round(cq_max + sec_max, 2)} · ⏱ {dur:.2f}s</span>
    </summary>
    {skip_note}
    <div style="margin-top:12px;display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
        <div style="background:#f8fafc;padding:10px 12px;display:flex;align-items:center;justify-content:space-between">
          <span style="font-weight:600;font-size:13px;color:#475569">📏 代码质量 {cq_badge}</span>
          <span style="font-size:18px;font-weight:700;color:{cq_col}">{cq_score}<span style="font-size:12px;color:#94a3b8">/{cq_max}</span></span>
        </div>
        <div style="padding:0 12px 4px">
          <div style="height:6px;background:#e2e8f0;border-radius:3px;margin:8px 0 10px">
            <div style="height:100%;width:{cq_pct}%;background:{cq_col};border-radius:3px"></div>
          </div>
          {cq_detail}
        </div>
      </div>
      <div style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
        <div style="background:#f8fafc;padding:10px 12px;display:flex;align-items:center;justify-content:space-between">
          <span style="font-weight:600;font-size:13px;color:#475569">🔒 安全合规 {sec_badge}</span>
          <span style="font-size:18px;font-weight:700;color:{sec_col}">{sec_score:.2f}<span style="font-size:12px;color:#94a3b8">/{sec_max}</span></span>
        </div>
        <div style="padding:0 12px 4px">
          <div style="height:6px;background:#e2e8f0;border-radius:3px;margin:8px 0 10px">
            <div style="height:100%;width:{sec_pct}%;background:{sec_col};border-radius:3px"></div>
          </div>
          {sec_detail}
        </div>
      </div>
    </div>
  </details>
</div>"""

    def _html_layer4(self, l4: dict) -> str:  # pylint: disable=too-many-locals
        if not l4 or l4.get("status") not in ("completed", "partial"):
            return ""
        e = self._esc
        sc = self._score_color
        per_case = l4.get("per_case", []) or []
        _ = l4.get("execution_breakdown", {}) or {}
        robust_s = l4.get("robust_score", 0)
        robust_m = l4.get("robust_max", 8)
        correct_s = l4.get("correct_score", 0)
        correct_m = l4.get("correct_max", 22)
        delta_s = l4.get("delta_score", 0)
        delta_m = l4.get("delta_max", 0)

        def bar_row(label: str, s: float, m: float, color: str) -> str:
            pct = round(s / max(m, 0.01) * 100)
            return (f"<div style='margin-bottom:8px'>"
                    f"<div style='display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px'>"
                    f"<span style='font-weight:600'>{label}</span>"
                    f"<span style='color:{color};font-weight:700'>{s} / {m} ({pct}%)</span></div>"
                    f"<div class='bar-wrap'><div class='bar-fill' style='background:{color};width:{pct}%'></div></div>"
                    f"</div>")

        sub_bars = bar_row("健壮性", robust_s, robust_m, sc(round(robust_s/max(robust_m,1)*100)))
        sub_bars += bar_row("正确性", correct_s, correct_m, sc(round(correct_s/max(correct_m,1)*100)))
        if delta_m > 0:
            sub_bars += bar_row("增量价值", delta_s, delta_m, sc(round(delta_s/max(delta_m,1)*100)))

        # Classify test cases into three groups: success / partial / fail.
        # A case is "fail" when it has a hard execution failure that produces no usable
        # output. This includes: timeout, stub, invoke failure, API error, empty output,
        # or error-like output (model returned an error message instead of real content).
        # A case is "partial" when it has soft issues (incomplete_response with some
        # output, or short output) — the model produced something but it's degraded.
        # Everything else counts as "success".
        def _classify_case(case: dict) -> str:
            with_run = case.get("with") or {}
            execution = with_run.get("execution", {}) or {}
            failure_tags = execution.get("failure_tags", []) or []
            with_status = with_run.get("status", "")
            hard_failure_tags = {
                "tc_timeout", "stub_output", "invoke_failed", "api_error",
                "tooling_failed", "empty_output", "error_in_output",
            }
            has_hard_failure = bool(hard_failure_tags & set(failure_tags))
            if has_hard_failure or with_status == "error":
                return "fail"
            soft_failure_tags = {"incomplete_response", "short_output"}
            has_soft_failure = bool(soft_failure_tags & set(failure_tags))
            if has_soft_failure:
                return "fail"
            return "success"

        def _get_failure_reason(case: dict) -> str:
            """Extract a human-readable failure reason for a case."""
            with_run = case.get("with") or {}
            execution = with_run.get("execution", {}) or {}
            failure_tags = execution.get("failure_tags", []) or []
            failure_reason = execution.get("failure_reason", "")
            with_status = with_run.get("status", "")
            duration = with_run.get("invoke_duration_s", with_run.get("duration_seconds", 0)) or 0
            with_scores = with_run.get("scores", {}) or {}
            robust_raw = with_scores.get("robust_raw", 0)
            correct_raw = with_scores.get("correct_raw", 0)

            reasons = []
            if duration >= 590:
                reasons.append("执行超时（≥600s）")
            if "empty_output" in failure_tags or "not_empty" in failure_tags:
                reasons.append("输出为空")
            if "short_output" in failure_tags:
                reasons.append("输出过短（无实质内容）")
            if "error_in_output" in failure_tags:
                reasons.append("输出包含错误信息")
            if "incomplete_response" in failure_tags:
                reasons.append("回答不完整/含反问")
            if "stub_output" in failure_tags:
                reasons.append("桩输出（未真正调用 Skill）")
            if with_status == "error":
                reasons.append("执行异常")
            known_tags = {"empty_output", "not_empty", "incomplete_response", "stub_output",
                          "short_output", "error_in_output"}
            remaining_tags = [t for t in failure_tags if t not in known_tags]
            if remaining_tags and not reasons:
                reasons.append(f"失败标签: {', '.join(remaining_tags)}")
            if failure_reason and failure_reason not in " ".join(reasons):
                reasons.append(failure_reason)
            # Partial-fail specific reasons
            if not failure_tags and with_status != "error":
                if robust_raw < 0.5:
                    reasons.append(f"健壮性不足（{robust_raw:.2f}）")
                if correct_raw < 0.5:
                    reasons.append(f"正确性不足（{correct_raw:.2f}）")
            return "；".join(reasons) if reasons else "原因待定"

        success_cases = [c for c in per_case if _classify_case(c) == "success"]
        failed_cases = [c for c in per_case if _classify_case(c) == "fail"]

        success_html = "".join(self._html_tc_card(c, e, sc) for c in success_cases)
        fail_html = "".join(
            self._html_tc_card(c, e, sc, show_correctness=False, failure_reason=_get_failure_reason(c))
            for c in failed_cases
        )

        success_section = f"""
<details class='tc-card' open>
  <summary class='tc-header'>
    <span class='badge pass'>✅ 运行成功</span>
    <span style='font-weight:600;font-size:13px;flex:1'>成功用例</span>
    <span style='font-size:12px;color:#64748b'>共 {len(success_cases)} 个</span>
    <span style='color:#94a3b8;font-size:11px;margin-left:6px'>▶ 点击折叠</span>
  </summary>
  <div class='tc-body' style='display:block;padding:10px 12px'>
    {success_html if success_html else "<div style='font-size:12px;color:#94a3b8'>无成功用例</div>"}
  </div>
</details>""" if success_cases else ""

        fail_section = f"""
<details class='tc-card' open>
  <summary class='tc-header'>
    <span class='badge fail'>❌ 运行失败</span>
    <span style='font-weight:600;font-size:13px;flex:1'>失败用例</span>
    <span style='font-size:12px;color:#64748b'>共 {len(failed_cases)} 个 · 执行层面失败，无正确性评估</span>
    <span style='color:#94a3b8;font-size:11px;margin-left:6px'>▶ 点击折叠</span>
  </summary>
  <div class='tc-body' style='display:block;padding:10px 12px'>
    {fail_html}
  </div>
</details>""" if failed_cases else ""

        # Summary stats — two columns
        summary_html = f"""
<div style="margin:10px 0 16px 0;display:grid;grid-template-columns:1fr 1fr;gap:12px">
  <div style="border:1px solid #dcfce7;border-radius:8px;padding:10px;background:#f0fdf4">
    <div style="font-size:12px;font-weight:700;color:#166534;margin-bottom:6px">✅ 运行成功</div>
    <div style="font-size:24px;font-weight:800;color:#16a34a">{len(success_cases)}</div>
  </div>
  <div style="border:1px solid #fee2e2;border-radius:8px;padding:10px;background:#fef2f2">
    <div style="font-size:12px;font-weight:700;color:#991b1b;margin-bottom:6px">❌ 运行失败</div>
    <div style="font-size:24px;font-weight:800;color:#dc2626">{len(failed_cases)}</div>
  </div>
</div>"""

        # Build failure root cause analysis and skill optimization suggestions
        root_cause_html = ""
        suggestions_html = ""
        if failed_cases:
            # Analyze failure root causes
            cause_groups: dict[str, list[str]] = {}
            for fc in failed_cases:
                fc_id = fc.get("tc_id", "")
                w = fc.get("with") or {}
                exe = w.get("execution", {}) or {}
                tags = exe.get("failure_tags", []) or []
                _ = exe.get("failure_reason", "")
                duration = w.get("invoke_duration_s", w.get("duration_seconds", 0)) or 0

                if duration >= 590:
                    cause_groups.setdefault("timeout", []).append(fc_id)
                elif "empty_output" in tags or "not_empty" in tags:
                    cause_groups.setdefault("empty_output", []).append(fc_id)
                elif "error_in_output" in tags:
                    cause_groups.setdefault("error_in_output", []).append(fc_id)
                elif "short_output" in tags:
                    cause_groups.setdefault("short_output", []).append(fc_id)
                elif "incomplete_response" in tags:
                    cause_groups.setdefault("incomplete_response", []).append(fc_id)
                elif "stub_output" in tags:
                    cause_groups.setdefault("stub_output", []).append(fc_id)
                elif tags:
                    cause_groups.setdefault("other_failure", []).append(fc_id)
                else:
                    # Check correctness scores
                    scores = w.get("scores", {}) or {}
                    if scores.get("correct_raw", 1) < 0.5:
                        cause_groups.setdefault("low_correctness", []).append(fc_id)
                    else:
                        cause_groups.setdefault("unknown", []).append(fc_id)

            cause_labels = {
                "timeout": ("⏱ 执行超时", "用例执行超过 600s 硬限制被强制终止，通常因为 agent loop 迭代轮数过多或数据量过大"),
                "empty_output": ("📭 输出为空", "模型未返回有效内容，可能是 tool call 失败或 prompt 未被正确理解"),
                "error_in_output": ("🚫 输出包含错误", "模型返回了错误信息而非有效结果，可能是脚本执行失败、权限不足或环境依赖缺失"),
                "short_output": ("📏 输出过短", "模型返回内容不足 20 字符，无实质性内容，可能是 prompt 理解偏差或执行中断"),
                "incomplete_response": ("❓ 回答不完整/含反问", "模型回答末尾追加了确认性问句，被标记为 incomplete_response"),
                "stub_output": ("🔇 桩输出", "执行路径降级为桩模式，未真正调用 Skill"),
                "low_correctness": ("📉 正确性不足", "模型返回了内容但分析结果与预期不符，正确性得分低于 0.5"),
                "other_failure": ("⚠️ 其他执行失败", "执行过程中出现非超时类错误"),
                "unknown": ("❔ 原因待定", "未匹配到已知失败模式"),
            }

            cause_rows = []
            for cause_key, tc_ids in cause_groups.items():
                label, desc = cause_labels.get(cause_key, ("❔ 未知", ""))
                ids_str = ", ".join(f"<span class='mono'>{e(tid)}</span>" for tid in tc_ids)
                cause_rows.append(
                    f"<tr><td style='white-space:nowrap'>{label}</td>"
                    f"<td style='font-size:12px;color:#64748b'>{e(desc)}</td>"
                    f"<td style='font-size:12px'><b>{len(tc_ids)}</b> 条</td>"
                    f"<td style='font-size:11px'>{ids_str}</td></tr>"
                )

            root_cause_html = f"""
<div style="margin-top:16px;border-top:2px solid #e2e8f0;padding-top:16px">
  <div style="font-size:14px;font-weight:700;color:#475569;margin-bottom:10px">🔍 失败用例根因分析</div>
  <table>
    <thead><tr><th>失败类型</th><th>说明</th><th>数量</th><th>涉及用例</th></tr></thead>
    <tbody>{''.join(cause_rows)}</tbody>
  </table>
</div>"""

            # Build skill optimization suggestions based on root causes
            suggestions = []
            if "timeout" in cause_groups:
                n = len(cause_groups["timeout"])
                suggestions.append(
                    f"🔧 <b>超时优化（{n} 条）</b>：建议在 Skill 侧合并多步 tool call 为一站式脚本，"
                    "减少 agent loop 迭代轮数；对大数据量查询，在 prompt 中提示模型优先使用高效的 pandas 操作；"
                    "也可考虑提高 TC_TIMEOUT_S 配置"
                )
            if "incomplete_response" in cause_groups:
                n = len(cause_groups["incomplete_response"])
                suggestions.append(
                    f"📝 <b>回答完整性（{n} 条）</b>：模型在回答末尾追加了确认性问句被误判为不完整。"
                    "建议在 Skill 的 system prompt 中明确要求模型「直接给出分析结果，不要在末尾追加确认性问句」"
                )
            if "empty_output" in cause_groups:
                n = len(cause_groups["empty_output"])
                suggestions.append(
                    f"📭 <b>空输出处理（{n} 条）</b>：建议检查 Skill 的 tool 注册是否正确，"
                    "确保模型能正确识别并调用对应的分析脚本"
                )
            if "low_correctness" in cause_groups:
                n = len(cause_groups["low_correctness"])
                suggestions.append(
                    f"📉 <b>正确性提升（{n} 条）</b>：模型返回了内容但分析结果不准确。"
                    "建议优化 Skill 的数据分析 prompt，增加对字段含义的说明，"
                    "并在 context 中提供更明确的表结构和字段映射关系"
                )
            if len(failed_cases) > len(per_case) * 0.5:
                suggestions.append(
                    "⚡ <b>整体通过率偏低</b>：建议优先确保核心功能（单表查询、简单聚合）稳定可靠后再扩展复杂场景"
                )

            if suggestions:
                items = "".join(
                    f"<li style='margin-bottom:8px;padding:8px 12px;background:#f8fafc;border-radius:6px;"
                    f"font-size:13px;border-left:3px solid #3b82f6;line-height:1.6'>{s}</li>"
                    for s in suggestions
                )
                suggestions_html = f"""
<div style="margin-top:16px;border-top:1px solid #e2e8f0;padding-top:14px">
  <div style="font-size:14px;font-weight:700;color:#475569;margin-bottom:10px">🛠 Skill 优化建议</div>
  <ul style="padding-left:0;list-style:none">{items}</ul>
</div>"""

        return f"""
<div class="card">
  <details open>
    <summary class="card-title">
      🧪 Layer 4 — 动态测试详情 &nbsp;
      <span style='font-size:13px;color:#64748b'>用例数: {len(per_case)} · 成功 {len(success_cases)} · 失败 {len(failed_cases)} · 得分 {round(robust_s + correct_s + delta_s, 2)}/{round(robust_m + correct_m + delta_m, 2)}</span>
    </summary>
    <div style="margin-top:14px">
      <div style="margin-bottom:16px;max-width:400px">{sub_bars}</div>
      {summary_html}
      {fail_section}
      {success_section}
      {root_cause_html}
      {suggestions_html}
    </div>
  </details>
</div>"""

    @staticmethod
    def _html_tc_card(c: dict, e, sc, *, show_correctness: bool = True, failure_reason: str = "") -> str:  # pylint: disable=too-many-locals
        del sc
        tc_id = c.get("tc_id", "")
        _ = c.get("priority", "P1").lower()
        with_run = c.get("with") or {}
        without_run = c.get("without") or {}
        has_without = bool(without_run and (without_run.get("output") or without_run.get("status")))

        # Scores
        with_scores = with_run.get("scores", {}) or {}
        robust_raw = with_scores.get("robust_raw", 0)
        correct_raw = with_scores.get("correct_raw", 0)
        robust_ok = robust_raw >= 0.5
        robust_badge = "<span class='badge pass'>健壮 ✓</span>" if robust_ok else "<span class='badge fail'>健壮 ✗</span>"

        # Timing
        with_s = with_run.get("invoke_duration_s", with_run.get("duration_seconds", 0)) or 0
        without_s = without_run.get("invoke_duration_s", without_run.get("duration_seconds", 0)) or 0
        with_badge = f"<span class='badge info'>⏱ with:{with_s:.2f}s</span>"
        without_badge = f"<span class='badge warn'>⏱ without:{without_s:.2f}s</span>" if has_without else ""

        # Token consumption
        with_tokens = with_run.get("token_count", 0) or 0
        without_tokens = (without_run.get("token_count", 0) or 0) if has_without else 0
        token_badge = f"<span class='badge info'>🪙 with:{with_tokens:,} tokens</span>" if with_tokens else ""
        without_token_badge = f"<span class='badge warn'>🪙 without:{without_tokens:,} tokens</span>" if has_without and without_tokens else ""

        # Input
        prompt = with_run.get("input", {}).get("prompt", "") or ""

        # With-skill output (no truncation — pre block is scrollable)
        with_out = with_run.get("output", {}) or {}
        with_raw = with_out.get("raw_response") or with_out.get("text") or ""
        with_status = with_run.get("status", "")
        run_method = with_out.get("simulation_note", with_out.get("method", with_out.get("run_mode", "")))
        execution = with_run.get("execution", {}) or {}
        route_label = execution.get("route_label", "")
        _ = execution.get("attempts", []) or []
        failure_tags = execution.get("failure_tags", []) or []
        failure_reason = execution.get("failure_reason", "")
        exit_code = with_out.get("exit_code")
        with_info = ""
        if run_method or route_label:
            detail_line = run_method or route_label
            with_info = f"<div style='font-size:11px;color:#64748b;margin-bottom:4px'>ℹ️ {e(detail_line)}"
            if exit_code is not None:
                with_info += f" · exit {exit_code}"
            with_info += "</div>"
            if failure_tags:
                with_info += (
                    f"<div style='font-size:11px;color:#b45309;margin-bottom:4px'>"
                    f"⚠️ 失败标签: {e(','.join(failure_tags))}"
                    f"{(' · ' + e(failure_reason)) if failure_reason else ''}</div>"
                )
        elif with_status == "error":
            with_info = "<div style='font-size:11px;color:#f97316;margin-bottom:4px'>⚠️ 执行异常</div>"
        # Show without_skill simulation note (only if without data exists)
        without_sim_note = ""
        without_raw = ""
        without_note = ""
        if has_without:
            without_out2 = without_run.get("output", {}) or {}
            ws_note = without_out2.get("simulation_note", "")
            if ws_note:
                without_sim_note = f"<div style='font-size:11px;color:#9a3412;margin-bottom:4px'>ℹ️ {e(ws_note)}</div>"
            without_out = without_run.get("output", {}) or {}
            without_raw = without_out.get("raw_response") or without_out.get("text") or ""
            if not without_raw:
                without_note = "<div style='font-size:11px;color:#f97316;margin-bottom:4px'>⚠️ STUB 占位输出（待接入真实 API）</div>"
                without_raw = "[本地模式 baseline — 跳过]"

        # Robustness checks
        robustness_rows = []
        for r in with_run.get("robustness_results", []):
            icon = "✅" if r.get("passed") else "❌"
            robustness_rows.append(
                f"<tr><td>{icon}</td>"
                f"<td class='mono'>{e(r.get('check_type', r.get('check_id', '')))}</td>"
                f"<td style='font-size:12px;color:#475569'>{e(str(r.get('detail','')))}</td></tr>"
            )

        # Correctness assertions
        correct_rows = []
        for r in with_run.get("correctness_results", []):
            s_val = r.get("score", 0)
            level = r.get("level", "")
            reasoning = r.get("reasoning", r.get("detail", ""))
            stars_filled = round(s_val * 5)
            stars = "".join(["<span style='color:#fbbf24'>★</span>"] * stars_filled +
                            ["<span style='color:#e2e8f0'>★</span>"] * (5 - stars_filled))
            lvl_badge = (f"<span class='badge pass'>{e(level)}</span>" if s_val >= 0.7
                         else f"<span class='badge fail'>{e(level)}</span>")
            method = r.get("eval_method", "规则")
            method_label = "LLM" if "llm" in method.lower() else "规则"
            score_col = "#22c55e" if s_val >= 0.7 else "#ef4444"
            correct_rows.append(
                f"<tr><td style='font-size:12px;max-width:280px'>{e(r.get('criterion',''))}"
                f"<span style='font-size:10px;background:#fef9c3;color:#854d0e;border-radius:3px;"
                f"padding:1px 4px;margin-left:4px'>{method_label}</span></td>"
                f"<td><span style='font-weight:700;color:{score_col}'>{s_val:.2f}</span> {stars}</td>"
                f"<td>{lvl_badge}</td>"
                f"<td style='font-size:12px;color:#475569;max-width:320px'>{e(str(reasoning))}</td></tr>"
            )

        # Delta comparison (only if without_run has real data)
        has_baseline = has_without and bool(without_run.get("output", {}).get("raw_response"))
        without_scores = (without_run.get("scores", {}) or {}) if has_without else {}
        w_robust = without_scores.get("robust_raw", 0)
        w_correct = without_scores.get("correct_raw", 0)
        d_robust = robust_raw - w_robust
        d_correct = correct_raw - w_correct

        if has_baseline:
            delta_robust_cls = "delta-pos" if d_robust > 0 else ("delta-neg" if d_robust < 0 else "delta-neu")
            delta_correct_cls = "delta-pos" if d_correct > 0 else ("delta-neg" if d_correct < 0 else "delta-neu")
            compare_html = f"""
<div style='margin-top:12px'>
  <div style='font-size:12px;font-weight:700;color:#475569;margin-bottom:6px'>📊 With Skill vs Without Skill 对比</div>
  <div class='compare-grid'>
    <div class='compare-col with-col'>
      <div style='font-weight:700;color:#166534;margin-bottom:6px'>🟢 With Skill（使用技能）</div>
      <div style='font-size:13px'>健壮性原始分：<strong>{robust_raw:.3f}</strong></div>
      <div style='font-size:13px'>正确性原始分：<strong>{correct_raw:.3f}</strong></div>
      <div style='margin-top:6px'><div class='bar-wrap'>
        <div class='bar-fill' style='background:#22c55e;width:{round(correct_raw*100)}%'></div></div></div>
    </div>
    <div class='compare-col without-col'>
      <div style='font-weight:700;color:#9a3412;margin-bottom:6px'>🔴 Without Skill（基线）</div>
      <div style='font-size:13px'>健壮性原始分：<strong>{w_robust:.3f}</strong></div>
      <div style='font-size:13px'>正确性原始分：<strong>{w_correct:.3f}</strong></div>
      <div style='margin-top:6px'><div class='bar-wrap'>
        <div class='bar-fill' style='background:#f97316;width:{round(w_correct*100)}%'></div></div></div>
    </div>
  </div>
  <div style='margin-top:8px;display:flex;gap:20px;font-size:13px'>
    <span>Δ 健壮性: <span class='{delta_robust_cls}'>{d_robust:+.3f}</span></span>
    <span>Δ 正确性: <span class='{delta_correct_cls}'>{d_correct:+.3f}</span></span>
  </div>
</div>"""
        elif has_without:
            compare_html = f"""
<div style='margin-top:12px'>
  <div style='font-size:12px;font-weight:700;color:#475569;margin-bottom:6px'>📊 With Skill vs Without Skill 对比</div>
  <div class='compare-grid'>
    <div class='compare-col with-col'>
      <div style='font-weight:700;color:#166534;margin-bottom:6px'>🟢 With Skill（使用技能）</div>
      <div style='font-size:13px'>健壮性原始分：<strong>{robust_raw:.3f}</strong></div>
      <div style='font-size:13px'>正确性原始分：<strong>{correct_raw:.3f}</strong></div>
      <div style='margin-top:6px'><div class='bar-wrap'>
        <div class='bar-fill' style='background:#22c55e;width:{round(robust_raw*100)}%'></div></div></div>
    </div>
    <div class='compare-col without-col'>
      <div style='font-weight:700;color:#9a3412;margin-bottom:6px'>🔴 Without Skill（基线）</div>
      <div style='font-size:13px'>健壮性原始分：<strong>{w_robust:.3f}</strong></div>
      <div style='font-size:13px'>正确性原始分：<strong>{w_correct:.3f}</strong></div>
      <div style='margin-top:6px'><div class='bar-wrap'>
        <div class='bar-fill' style='background:#f97316;width:{round(w_correct*100)}%'></div></div></div>
    </div>
  </div>
  <div style='margin-top:8px;display:flex;gap:20px;font-size:13px'>
    <span>Δ 健壮性: <span class='delta-neu'>{d_robust:+.3f}</span></span>
    <span>Δ 正确性: <span class='delta-neu'>{d_correct:+.3f}</span></span>
  </div>
</div>"""
        else:
            compare_html = ""

        # Build failure reason note if provided
        failure_reason_html = ""
        if failure_reason:
            failure_reason_html = (
                f"<div style='margin-top:8px;padding:8px 12px;background:#fef2f2;border-left:3px solid #ef4444;"
                f"border-radius:4px;font-size:12px;color:#991b1b'>"
                f"<strong>❌ 失败原因：</strong>{e(failure_reason)}</div>"
            )

        # Conditionally render correctness section
        correctness_section = ""
        if show_correctness:
            correctness_section = f"""
    <div style='margin-top:12px'>
      <div style='font-size:12px;font-weight:700;color:#475569;margin-bottom:6px'>🎯 正确性断言</div>
      <table><tr><th>评估标准</th><th>得分</th><th>等级</th><th>评判理由</th></tr>
      {(''.join(correct_rows)) if correct_rows else "<tr><td colspan=4 style='color:#94a3b8'>无评估数据</td></tr>"}
      </table>
    </div>"""
        else:
            correctness_section = """
    <div style='margin-top:12px;padding:10px;background:#f8fafc;border-radius:6px;border:1px dashed #cbd5e1'>
      <span style='font-size:12px;color:#94a3b8'>⏭ 执行层面失败，跳过正确性评估</span>
    </div>"""

        return f"""
<details class='tc tc-card'>
  <summary class='tc-header'>
    <span style='font-weight:600;font-size:13px;flex:1'>{e(tc_id)}</span>
    {robust_badge}
    <span style='font-size:12px;color:#64748b'>正确性 {correct_raw:.2f}</span>
    {with_badge} {without_badge} {token_badge} {without_token_badge}
    <span style='color:#94a3b8;font-size:11px;margin-left:6px'>▶ 点击展开</span>
  </summary>
  <div class='tc-body'>
    {failure_reason_html}
    <div style='margin-top:10px'>
      <div style='font-size:12px;font-weight:700;color:#475569;margin-bottom:4px'>📥 输入 Prompt</div>
      <pre>{e(prompt)}</pre>
    </div>
    <div style='margin-top:8px'>
      <div style='font-size:12px;font-weight:700;color:#15803d;margin-bottom:4px'>📤 With Skill 输出</div>
      {with_info}
      <pre style='background:#f0fdf4;color:#14532d;border:1px solid #86efac;overflow-y:auto;max-height:420px'>{e(with_raw) if with_raw else '(无输出)'}</pre>
    </div>
    {"<div style='margin-top:8px'>" +
      "<div style='font-size:12px;font-weight:700;color:#9a3412;margin-bottom:4px'>📤 Without Skill 输出（基线）</div>" +
      without_sim_note + without_note +
      "<pre style='background:#fff7ed;color:#7c2d12;border:1px solid #fed7aa;overflow-y:auto;max-height:420px'>" + (e(without_raw) if without_raw else '(无数据)') + "</pre>" +
    "</div>" if has_without else ""}
    <div style='margin-top:12px'>
      <div style='font-size:12px;font-weight:700;color:#475569;margin-bottom:6px'>🔩 健壮性检查</div>
      <table><tr><th>结果</th><th>检查类型</th><th>详情</th></tr>
      {(''.join(robustness_rows)) if robustness_rows else "<tr><td colspan=3 style='color:#94a3b8'>无检查数据</td></tr>"}
      </table>
    </div>
    {correctness_section}
    {compare_html}
  </div>
</details>"""

    def _html_case_analysis(self, d: dict) -> str:
        """Generate case execution result analysis section with success cases on top, failures below, plus optimization suggestions."""
        e = self._esc
        test_cases = d.get("test_cases", []) or []
        if not test_cases:
            return ""

        success_cases = [tc for tc in test_cases if tc.get("result") == "pass"]
        partial_cases = [tc for tc in test_cases if tc.get("result") == "partial"]
        fail_cases = [tc for tc in test_cases if tc.get("result") == "fail"]

        total = len(test_cases)
        pass_count = len(success_cases)
        partial_count = len(partial_cases)
        fail_count = len(fail_cases)
        pass_rate = round(pass_count / total * 100, 1) if total else 0

        priority_css = {"P0": "p0", "P1": "p1", "P2": "p2"}
        result_badge = {
            "pass": "<span class='badge pass'>✅ 通过</span>",
            "partial": "<span class='badge warn'>⚠️ 部分通过</span>",
            "fail": "<span class='badge fail'>❌ 失败</span>",
        }

        def _render_case_row(tc: dict) -> str:
            tc_id = e(tc.get("id", ""))
            prio = tc.get("priority", "P2")
            prio_cls = priority_css.get(prio, "p2")
            result = tc.get("result", "fail")
            badge = result_badge.get(result, result_badge["fail"])
            er = tc.get("execution_record", {}) or {}
            method = e(er.get("method", "—"))
            duration_ms = er.get("duration_ms", 0) or 0
            duration_s = round(duration_ms / 1000, 1)
            assertions = tc.get("assertions", []) or []
            passed_count = sum(1 for a in assertions if a.get("passed"))
            total_asserts = len(assertions)
            desc = e(tc.get("description", ""))

            _ = ""
            output_text = er.get("output", "")
            if isinstance(output_text, str) and len(output_text) > 0:
                preview = output_text[:300].replace("<", "&lt;").replace(">", "&gt;")
                if len(output_text) > 300:
                    preview += "..."
                _ = f"<div style='margin-top:8px'><div style='font-size:11px;color:#64748b;margin-bottom:4px'>输出预览：</div><pre style='font-size:11px;max-height:120px'>{preview}</pre></div>"

            _ = ""
            if result == "fail" and duration_s >= 599:
                _ = "<div style='margin-top:6px;padding:6px 10px;background:#fee2e2;border-radius:4px;font-size:12px;color:#991b1b'>⏱ 超时（执行超过 600s 硬限制被强制终止）</div>"
            elif result == "partial":
                failed_asserts = [a for a in assertions if not a.get("passed")]
                if failed_asserts:
                    _ = f"<div style='margin-top:6px;padding:6px 10px;background:#fef9c3;border-radius:4px;font-size:12px;color:#92400e'>部分断言未通过（{len(failed_asserts)}/{total_asserts}）</div>"

            return f"""<tr>
  <td><span class='mono'>{tc_id}</span></td>
  <td><span class='badge {prio_cls}'>{e(prio)}</span></td>
  <td>{badge}</td>
  <td style='font-size:12px'>{desc}</td>
  <td style='font-size:12px'>{passed_count}/{total_asserts}</td>
  <td style='font-size:12px'>{duration_s}s</td>
  <td style='font-size:11px;color:#94a3b8;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{method}</td>
</tr>"""

        def _render_section(title: str, icon: str, cases: list, bg_color: str, border_color: str) -> str:
            if not cases:
                return ""
            rows = "".join(_render_case_row(tc) for tc in cases)
            return f"""
<div style="margin-bottom:16px;border:1px solid {border_color};border-radius:8px;overflow:hidden">
  <div style="background:{bg_color};padding:10px 14px;font-size:13px;font-weight:700;color:#1e293b">
    {icon} {e(title)}（{len(cases)} 条）
  </div>
  <table>
    <thead><tr>
      <th>用例 ID</th><th>优先级</th><th>结果</th><th>描述</th><th>断言</th><th>耗时</th><th>执行路径</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

        success_html = _render_section("成功用例", "✅", success_cases, "#f0fdf4", "#bbf7d0")
        partial_html = _render_section("部分通过用例", "⚠️", partial_cases, "#fffbeb", "#fde68a")
        fail_html = _render_section("失败用例", "❌", fail_cases, "#fef2f2", "#fecaca")

        timeout_count = sum(1 for tc in fail_cases
                           if (tc.get("execution_record", {}) or {}).get("duration_ms", 0) >= 599000)

        suggestions = []
        if timeout_count > 0:
            suggestions.append(
                f"🔧 <b>超时优化</b>：{timeout_count} 条用例因超过 600s 超时限制而失败。"
                "建议：① 提高 TC_TIMEOUT_S 至 900s 或 1200s；"
                "② 在 Skill 侧合并多步 tool call 为一站式脚本，减少 agent loop 迭代轮数；"
                "③ 对大数据量查询，在 prompt 中提示模型优先使用高效的 pandas 操作"
            )
        if partial_count > 0:
            _ = [tc for tc in partial_cases
                                if any(not a.get("passed") for a in (tc.get("assertions", []) or []))]
            suggestions.append(
                f"📝 <b>部分通过分析</b>：{partial_count} 条用例部分通过。"
                "常见原因：模型回答末尾追加了反问句被误判为 incomplete_response，或部分分析维度缺失。"
                "建议：① 优化 incomplete_response 检测逻辑，区分「回答完整但末尾有追问」和「真正的中途停止」；"
                "② 在 Skill prompt 中明确要求模型不要在回答末尾追加确认性问句"
            )
        if fail_count > 0 and timeout_count < fail_count:
            non_timeout_fails = fail_count - timeout_count
            if non_timeout_fails > 0:
                suggestions.append(
                    f"🔍 <b>非超时失败</b>：{non_timeout_fails} 条用例因非超时原因失败。"
                    "建议：检查 Skill 对复杂查询的支持能力，考虑拆分复杂用例为多个简单子查询"
                )
        if pass_rate < 50:
            suggestions.append(
                "⚡ <b>整体通过率偏低</b>：建议优先优化 P0 用例的通过率，"
                "确保核心功能（单表查询、简单聚合）稳定可靠后再扩展复杂场景"
            )

        suggestions_html = ""
        if suggestions:
            items = "".join(
                f"<li style='margin-bottom:8px;padding:8px 12px;background:#f8fafc;border-radius:6px;"
                f"font-size:13px;border-left:3px solid #3b82f6;line-height:1.6'>{s}</li>"
                for s in suggestions
            )
            suggestions_html = f"""
<div style="margin-top:16px;border-top:1px solid #e2e8f0;padding-top:14px">
  <div style="font-size:13px;font-weight:700;color:#475569;margin-bottom:10px">🛠 评测侧优化建议</div>
  <ul style="padding-left:0;list-style:none">{items}</ul>
</div>"""

        return f"""
<div class="card">
  <div class="card-title">📊 用例执行结果分析</div>
  <div style="display:flex;gap:16px;margin-bottom:16px">
    <div style="flex:1;text-align:center;padding:12px;background:#f0fdf4;border-radius:8px;border:1px solid #bbf7d0">
      <div style="font-size:28px;font-weight:800;color:#16a34a">{pass_count}</div>
      <div style="font-size:12px;color:#166534">通过</div>
    </div>
    <div style="flex:1;text-align:center;padding:12px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a">
      <div style="font-size:28px;font-weight:800;color:#d97706">{partial_count}</div>
      <div style="font-size:12px;color:#92400e">部分通过</div>
    </div>
    <div style="flex:1;text-align:center;padding:12px;background:#fef2f2;border-radius:8px;border:1px solid #fecaca">
      <div style="font-size:28px;font-weight:800;color:#dc2626">{fail_count}</div>
      <div style="font-size:12px;color:#991b1b">失败</div>
    </div>
    <div style="flex:1;text-align:center;padding:12px;background:#f1f5f9;border-radius:8px;border:1px solid #e2e8f0">
      <div style="font-size:28px;font-weight:800;color:#475569">{pass_rate}%</div>
      <div style="font-size:12px;color:#64748b">通过率</div>
    </div>
  </div>
  {success_html}
  {partial_html}
  {fail_html}
  {suggestions_html}
</div>"""

    def _html_baseline_comparison(self, d: dict) -> str:  # pylint: disable=too-many-locals
        """Render a standalone baseline comparison section with timing, tokens, and side-by-side responses."""
        ev = d.get("effect_validation")
        if not ev or not ev.get("per_case"):
            return ""

        e = self._esc
        sc = self._score_color
        per_case = ev["per_case"]
        has_any_without = any(c.get("has_without") for c in per_case)
        if not has_any_without:
            return ""

        # --- Overall verdict banner ---
        verdict = ev.get("verdict", "NEGATIVE")
        delta_score = ev.get("delta_score", 0)
        delta_max = ev.get("delta_max", 0)
        with_rate = ev.get("with_skill_pass_rate", 0)
        without_rate = ev.get("without_skill_pass_rate", 0)
        delta_pct = ev.get("delta", "+0%")
        verdict_color = "#16a34a" if verdict == "POSITIVE" else "#dc2626"
        verdict_icon = "🟢" if verdict == "POSITIVE" else "🔴"
        verdict_text = ev.get("verdict_text", "")

        verdict_html = f"""
<div style="display:flex;align-items:center;gap:16px;padding:16px;background:{'#f0fdf4' if verdict == 'POSITIVE' else '#fef2f2'};
            border:1px solid {'#bbf7d0' if verdict == 'POSITIVE' else '#fecaca'};border-radius:10px;margin-bottom:16px">
  <span style="font-size:36px">{verdict_icon}</span>
  <div style="flex:1">
    <div style="font-size:18px;font-weight:800;color:{verdict_color}">{e(verdict_text)}</div>
    <div style="font-size:13px;color:#64748b;margin-top:4px">
      With Skill 正确率 <strong>{with_rate:.0%}</strong> · Without Skill 正确率 <strong>{without_rate:.0%}</strong> · Delta <strong style="color:{verdict_color}">{e(delta_pct)}</strong>
    </div>
  </div>
  <div style="text-align:center">
    <div style="font-size:28px;font-weight:800;color:{verdict_color}">{delta_score}</div>
    <div style="font-size:11px;color:#94a3b8">/ {delta_max} 增值分</div>
  </div>
</div>"""

        # --- Aggregate timing & token comparison ---
        total_w_dur = ev.get("total_with_duration_s", 0)
        total_wo_dur = ev.get("total_without_duration_s", 0)
        dur_delta = total_w_dur - total_wo_dur
        dur_delta_cls = "delta-neg" if dur_delta > 0 else ("delta-pos" if dur_delta < 0 else "delta-neu")
        dur_delta_label = f"{dur_delta:+.1f}s"

        total_w_tok = ev.get("total_with_tokens", 0)
        total_wo_tok = ev.get("total_without_tokens", 0)
        tok_delta = total_w_tok - total_wo_tok
        tok_delta_cls = "delta-neg" if tok_delta > 0 else ("delta-pos" if tok_delta < 0 else "delta-neu")
        tok_delta_label = f"{tok_delta:+,}"

        aggregate_html = f"""
<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px">
  <div style="border:1px solid #e2e8f0;border-radius:10px;padding:16px">
    <div style="font-size:13px;font-weight:700;color:#475569;margin-bottom:12px">⏱ 耗时对比（总计）</div>
    <div style="display:flex;align-items:flex-end;gap:20px">
      <div style="flex:1">
        <div style="font-size:11px;color:#166534;font-weight:600;margin-bottom:4px">🟢 With Skill</div>
        <div style="font-size:24px;font-weight:800;color:#16a34a">{total_w_dur:.1f}<span style="font-size:13px;color:#94a3b8">s</span></div>
      </div>
      <div style="flex:1">
        <div style="font-size:11px;color:#9a3412;font-weight:600;margin-bottom:4px">🔴 Without Skill</div>
        <div style="font-size:24px;font-weight:800;color:#f97316">{total_wo_dur:.1f}<span style="font-size:13px;color:#94a3b8">s</span></div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;color:#64748b;margin-bottom:4px">差值</div>
        <div style="font-size:18px;font-weight:700" class="{dur_delta_cls}">{dur_delta_label}</div>
      </div>
    </div>
  </div>
  <div style="border:1px solid #e2e8f0;border-radius:10px;padding:16px">
    <div style="font-size:13px;font-weight:700;color:#475569;margin-bottom:12px">🪙 Token 消耗对比（总计）</div>
    <div style="display:flex;align-items:flex-end;gap:20px">
      <div style="flex:1">
        <div style="font-size:11px;color:#166534;font-weight:600;margin-bottom:4px">🟢 With Skill</div>
        <div style="font-size:24px;font-weight:800;color:#16a34a">{total_w_tok:,}<span style="font-size:13px;color:#94a3b8"> tok</span></div>
      </div>
      <div style="flex:1">
        <div style="font-size:11px;color:#9a3412;font-weight:600;margin-bottom:4px">🔴 Without Skill</div>
        <div style="font-size:24px;font-weight:800;color:#f97316">{total_wo_tok:,}<span style="font-size:13px;color:#94a3b8"> tok</span></div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;color:#64748b;margin-bottom:4px">差值</div>
        <div style="font-size:18px;font-weight:700" class="{tok_delta_cls}">{tok_delta_label}</div>
      </div>
    </div>
  </div>
</div>"""

        # --- Per-case comparison table ---
        case_rows = []
        for case in per_case:
            if not case.get("has_without"):
                continue
            tc_id = case.get("tc_id", "")
            prio = case.get("priority", "P1")
            prio_cls = prio.lower()

            w_dur = case.get("with_duration_s", 0)
            wo_dur = case.get("without_duration_s", 0)
            d_dur = w_dur - wo_dur
            d_dur_cls = "delta-neg" if d_dur > 0 else ("delta-pos" if d_dur < 0 else "delta-neu")

            w_tok = case.get("with_tokens", 0)
            wo_tok = case.get("without_tokens", 0)
            d_tok = w_tok - wo_tok
            d_tok_cls = "delta-neg" if d_tok > 0 else ("delta-pos" if d_tok < 0 else "delta-neu")

            w_correct = case.get("with_correct", 0)
            wo_correct = case.get("without_correct", 0)
            d_correct = w_correct - wo_correct
            d_correct_cls = "delta-pos" if d_correct > 0 else ("delta-neg" if d_correct < 0 else "delta-neu")

            case_rows.append(
                f"<tr>"
                f"<td><span class='badge {prio_cls}'>{e(prio)}</span> <span class='mono'>{e(tc_id)}</span></td>"
                f"<td style='text-align:right'>{w_dur:.1f}s</td>"
                f"<td style='text-align:right'>{wo_dur:.1f}s</td>"
                f"<td style='text-align:right' class='{d_dur_cls}'>{d_dur:+.1f}s</td>"
                f"<td style='text-align:right'>{w_tok:,}</td>"
                f"<td style='text-align:right'>{wo_tok:,}</td>"
                f"<td style='text-align:right' class='{d_tok_cls}'>{d_tok:+,}</td>"
                f"<td style='text-align:right'><span style='color:{sc(round(w_correct*100))}'>{w_correct:.2f}</span></td>"
                f"<td style='text-align:right'><span style='color:{sc(round(wo_correct*100))}'>{wo_correct:.2f}</span></td>"
                f"<td style='text-align:right' class='{d_correct_cls}'>{d_correct:+.2f}</td>"
                f"</tr>"
            )

        table_html = f"""
<div style="margin-bottom:18px">
  <div style="font-size:13px;font-weight:700;color:#475569;margin-bottom:8px">📋 逐用例对比明细</div>
  <div style="overflow-x:auto">
    <table style="font-size:12px;min-width:800px">
      <thead><tr>
        <th>用例</th>
        <th style='text-align:right'>With 耗时</th><th style='text-align:right'>Without 耗时</th><th style='text-align:right'>Δ 耗时</th>
        <th style='text-align:right'>With Token</th><th style='text-align:right'>Without Token</th><th style='text-align:right'>Δ Token</th>
        <th style='text-align:right'>With 正确性</th><th style='text-align:right'>Without 正确性</th><th style='text-align:right'>Δ 正确性</th>
      </tr></thead>
      <tbody>{''.join(case_rows)}</tbody>
    </table>
  </div>
</div>"""

        return f"""
<div class="card">
  <details open>
    <summary class="card-title">
      ⚖️ 增量价值分析 — With Skill vs Without Skill 基线对比 &nbsp;
      <span class='badge {"pass" if verdict == "POSITIVE" else "fail"}'>{verdict_icon} {e(verdict_text)}</span>
      &nbsp;<span style='font-size:13px;color:#64748b'>增值得分 {delta_score}/{delta_max}</span>
    </summary>
    <div style="margin-top:14px">
      {verdict_html}
      {aggregate_html}
      {table_html}
    </div>
  </details>
</div>"""

    def _html_findings(self, d: dict) -> str:
        e = self._esc
        recs = d.get("recommendations", []) or []
        findings = d.get("key_findings", {}) or {}
        strengths = findings.get("strengths", []) or []
        issues = findings.get("issues", []) or []
        if not recs and not strengths and not issues:
            return ""
        strengths_html = "".join(f"<li style='color:#166534;margin-bottom:4px'>✅ {e(s)}</li>" for s in strengths)
        issues_html = "".join(f"<li style='color:#b45309;margin-bottom:4px'>⚠️ {e(s)}</li>" for s in issues)
        recs_html = "".join(f"<li style='margin-bottom:6px;padding:6px 10px;background:#f8fafc;border-radius:4px;font-size:13px;border-left:3px solid #3b82f6'>{e(r)}</li>" for r in recs)
        return f"""
<div class="card">
  <div class="card-title">💡 发现 &amp; 建议</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:16px">
    <div>
      <div style="font-size:13px;font-weight:700;color:#166534;margin-bottom:8px">优势</div>
      <ul style="padding-left:18px;font-size:13px;line-height:1.7">{strengths_html or '<li style="color:#94a3b8">—</li>'}</ul>
    </div>
    <div>
      <div style="font-size:13px;font-weight:700;color:#b45309;margin-bottom:8px">待改进</div>
      <ul style="padding-left:18px;font-size:13px;line-height:1.7">{issues_html or '<li style="color:#94a3b8">—</li>'}</ul>
    </div>
  </div>
  <div style="border-top:1px solid #e2e8f0;padding-top:14px">
    <div style="font-size:13px;font-weight:700;color:#475569;margin-bottom:10px">改进建议</div>
    <ol style="padding-left:0;list-style:none;font-size:13px">{recs_html or '<li style="color:#94a3b8">暂无建议</li>'}</ol>
  </div>
</div>"""

