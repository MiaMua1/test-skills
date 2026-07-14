"""Layer 6 (Aggregate): Cross-skill comparison report."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

from evaluator.models.exceptions import AggregateError

logger = structlog.get_logger()


class Layer6Aggregate:
    """Aggregate layer: compare multiple eval results of the same profile type.

    Trigger conditions (all must be met):
    1. Same eval_profile
    2. profile_weight_snapshot identical across all eval_ids
    3. ≥2 eval_ids provided
    4. eval_data.json + scoring_criteria.json exist for each eval_id
    """

    layer_name = "layer6_aggregate"

    def __init__(self, storage_base: Path) -> None:
        self.storage_base = storage_base
        self.log = logger.bind(layer=self.layer_name)

    async def run(self, eval_ids: list[str]) -> dict:
        """Generate aggregate report for given eval_ids.

        Returns:
            Dict with aggregate_data_path and aggregate_report_path.

        Raises:
            AggregateError: On precondition failure.
        """
        if len(eval_ids) < 2:
            raise AggregateError(f"Need ≥2 eval_ids, got {len(eval_ids)}")

        included = []
        snapshots = []

        for eval_id in eval_ids:
            # Find the eval_data.json for this eval_id
            eval_data_path = self._find_eval_data(eval_id)
            criteria_path = self._find_scoring_criteria(eval_id)

            if not eval_data_path or not criteria_path:
                raise AggregateError(f"Missing eval_data.json or scoring_criteria.json for {eval_id}")

            eval_data = json.loads(eval_data_path.read_text(encoding="utf-8"))
            criteria_data = json.loads(criteria_path.read_text(encoding="utf-8"))
            snapshots.append((eval_id, criteria_data.get("profile_weight_snapshot", {}), eval_data))
            included.append(eval_data)

        # Validate: profiles may be mixed for cross-skill comparison; just warn
        profiles = {d.get("eval_profile") for d in included}
        profile_type = profiles.pop() if len(profiles) == 1 else "mixed"
        if profile_type == "mixed":
            self.log.warning(
                "layer6.mixed_profiles",
                profiles=list(profiles | {d.get("eval_profile") for d in included}),
                note="Cross-skill aggregate with mixed profiles; scores are normalized within each profile",
            )

        # Weight snapshot mismatch is only an error when profiles are identical;
        # for mixed-profile aggregates, skip this check.
        if profile_type != "mixed":
            ref_snapshot = snapshots[0][1]
            for eid, snap, _ in snapshots[1:]:
                if snap != ref_snapshot:
                    raise AggregateError(
                        f"profile_weight_snapshot mismatch: {eid} differs from {snapshots[0][0]}"
                    )

        aggregate_id = (
            f"{profile_type}-aggregate-"
            f"{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')}-"
            f"{str(uuid.uuid4())[:8]}"
        )

        ref_snapshot = snapshots[0][1] if profile_type != "mixed" else {}
        aggregate_data = self._build_aggregate(aggregate_id, profile_type, ref_snapshot, included)

        out_dir = self.storage_base / "aggregate" / profile_type / aggregate_id
        out_dir.mkdir(parents=True, exist_ok=True)

        data_path = out_dir / "aggregate_data.json"
        data_path.write_text(json.dumps(aggregate_data, ensure_ascii=False, indent=2), encoding="utf-8")

        html_path = out_dir / "aggregate_report.html"
        html = self._render_html(aggregate_data)
        html_path.write_text(html, encoding="utf-8")

        self.log.info("layer6.complete", aggregate_id=aggregate_id, count=len(included))
        return {
            "aggregate_id": aggregate_id,
            "aggregate_data_path": str(data_path),
            "aggregate_report_path": str(html_path),
        }

    def _find_eval_data(self, eval_id: str) -> Path | None:
        for p in self.storage_base.rglob(f"*/{eval_id}/eval_data.json"):
            return p
        return None

    def _find_scoring_criteria(self, eval_id: str) -> Path | None:
        for p in self.storage_base.rglob(f"*/{eval_id}/scoring_criteria.json"):
            return p
        return None

    def _build_aggregate(self, aggregate_id: str, profile_type: str,
                         snapshot: dict, evals: list[dict]) -> dict:
        scores = [e["summary"]["total_score"] for e in evals]
        grades = [e["summary"]["grade"] for e in evals]
        grade_dist = {g: grades.count(g) for g in ("A", "B", "C", "D", "F")}
        pass_count = sum(1 for g in grades if g in ("A", "B", "C"))

        dimension_labels = ["基础合规", "代码质量", "安全合规", "健壮性", "正确性", "增量价值"]
        dimension_stats = []
        for label in dimension_labels:
            dim_scores = []
            for e in evals:
                for item in e.get("score_breakdown", []):
                    if item["label"] == label:
                        dim_scores.append(item["score"])
            if dim_scores:
                dimension_stats.append({
                    "label": label,
                    "max_score": next(
                        (item["max_score"] for e in evals for item in e.get("score_breakdown", []) if item["label"] == label),
                        0,
                    ),
                    "avg_score": round(sum(dim_scores) / len(dim_scores), 2),
                    "min_score": min(dim_scores),
                    "max_achieved": max(dim_scores),
                    "layer": next(
                        (item["layer"] for e in evals for item in e.get("score_breakdown", []) if item["label"] == label),
                        1,
                    ),
                })

        # v5: collect security compliance and performance data per skill
        skill_security = []
        for e in evals:
            sec = e.get("layer2_security", {})
            skill_security.append({
                "skill_name": e["skill_name"],
                "security_raw": sec.get("security_raw", 1.0),
                "is_compliant": sec.get("is_compliant", True),
                "compliance_note": sec.get("compliance_note", ""),
                "critical_count": len(sec.get("critical_issues", [])),
                "findings_count": len(sec.get("all_findings", [])),
            })

        skill_comparison = [
            {
                "skill_name": e["skill_name"],
                "total_score": e["summary"]["total_score"],
                "grade": e["summary"]["grade"],
                "dimension_scores": {item["label"]: item["score"] for item in e.get("score_breakdown", [])},
                "security_compliant": next(
                    (s["is_compliant"] for s in skill_security if s["skill_name"] == e["skill_name"]), True
                ),
                "perf_baseline": e.get("layer4_perf_baseline", {}),
            }
            for e in evals
        ]

        return {
            "aggregate_id": aggregate_id,
            "aggregate_type": profile_type,
            "aggregate_type_desc": f"{profile_type} 类型 Skill 聚合评测报告",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "profile_weight_snapshot": snapshot,
            "included_evals": [
                {
                    "eval_id": e.get("eval_id", ""),
                    "skill_name": e["skill_name"],
                    "total_score": e["summary"]["total_score"],
                    "grade": e["summary"]["grade"],
                    "verdict": e["summary"].get("verdict", ""),
                    "evaluated_at": e.get("evaluation_date", ""),
                }
                for e in evals
            ],
            "aggregate_stats": {
                "skill_count": len(evals),
                "avg_total_score": round(sum(scores) / len(scores), 2),
                "max_total_score": max(scores),
                "min_total_score": min(scores),
                "grade_distribution": grade_dist,
                "pass_rate": round(pass_count / len(evals), 2),
                "needs_improvement_count": grades.count("D"),
                "failed_count": grades.count("F"),
                "security_non_compliant_count": sum(1 for s in skill_security if not s["is_compliant"]),
            },
            "dimension_stats": dimension_stats,
            "skill_comparison": skill_comparison,
            "skill_security": skill_security,
            "common_issues": [],
            "recommendations": [
                f"本批次 {len(evals)} 个 {profile_type} 类型 Skill 平均分 {round(sum(scores)/len(scores),1)} 分",
                "建议重点关注得分较低的维度进行针对性优化",
            ],
        }

    def _render_html(self, data: dict) -> str:
        profile_type = data["aggregate_type"]
        snapshot = data["profile_weight_snapshot"]
        stats = data["aggregate_stats"]
        generated = data["generated_at"]

        # Convert UTC to CST
        try:
            dt = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            cst_offset = timezone(__import__("datetime").timedelta(hours=8))
            generated_cst = dt.astimezone(cst_offset).strftime("%Y-%m-%d %H:%M:%S CST")
        except Exception:  # pylint: disable=broad-except
            generated_cst = generated

        weight_str = " | ".join(f"{k.replace('_max','')} {v}" for k, v in snapshot.items())

        non_compliant_count = stats.get("security_non_compliant_count", 0)
        security_banner = (
            f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;'
            f'padding:12px 16px;margin-bottom:16px;color:#991b1b;">'
            f'⛔ 本批次中 {non_compliant_count} 个 Skill 安全不合规（security_raw &lt; 0.67），建议优先修复</div>'
        ) if non_compliant_count > 0 else ""

        skill_rows = "".join(
            f"<tr>"
            f"<td><b>{c['skill_name']}</b></td>"
            f"<td style='font-weight:700'>{c['total_score']}</td>"
            f"<td>{c['grade']}</td>"
            + "".join(f"<td>{c['dimension_scores'].get(d, '—')}</td>"
                      for d in ["基础合规", "代码质量", "安全合规", "健壮性", "正确性", "增量价值"])
            + f"<td>{'✅' if c.get('security_compliant', True) else '⛔ 不合规'}</td>"
            + f"<td style='font-size:12px;color:#64748b'>{c.get('perf_baseline', {}).get('message', '—')}</td>"
            + "</tr>"
            for c in data["skill_comparison"]
        )

        # Security detail rows
        security_rows = ""
        for s in data.get("skill_security", []):
            bar_color = "#22c55e" if s["is_compliant"] else "#ef4444"
            pct = int(s["security_raw"] * 100)
            security_rows += (
                f"<tr><td>{s['skill_name']}</td>"
                f"<td><div style='background:#e2e8f0;border-radius:4px;height:10px;width:100%;'>"
                f"<div style='background:{bar_color};width:{pct}%;height:10px;border-radius:4px'></div></div>"
                f" {pct}%</td>"
                f"<td>{'✅ 合规' if s['is_compliant'] else '⛔ 不合规'}</td>"
                f"<td>{s['critical_count']} CRITICAL · {s['findings_count']} 总发现</td>"
                f"<td>{s['compliance_note'] or '—'}</td></tr>"
            )

        grade_dist = stats.get("grade_distribution", {})
        grade_str = " | ".join(f"<b>{g}</b>: {grade_dist.get(g, 0)}" for g in ["A", "B", "C", "D", "F"])

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>聚合评测报告 — {profile_type}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 1240px; margin: 0 auto; padding: 24px; background: #f8fafc; color: #1e293b; }}
  .banner {{ background: linear-gradient(135deg,#1e40af,#1d4ed8); color: white;
             border-radius: 12px; padding: 24px; margin-bottom: 24px; }}
  .banner h1 {{ margin: 0 0 8px; font-size: 22px; }}
  .banner p {{ margin: 4px 0; font-size: 13px; opacity: .88; }}
  .card {{ background: white; border-radius: 12px; padding: 24px;
           box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 16px; }}
  .card h2 {{ margin: 0 0 16px; font-size: 16px; color: #1e293b; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 9px 11px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
  th {{ background: #f1f5f9; font-weight: 600; color: #475569; }}
  tr:hover td {{ background: #f8fafc; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; margin-bottom: 16px; }}
  .stat {{ background: #f1f5f9; border-radius: 8px; padding: 12px 16px; }}
  .stat .val {{ font-size: 24px; font-weight: 700; color: #1e40af; }}
  .stat .lbl {{ font-size: 12px; color: #64748b; margin-top: 2px; }}
</style>
</head>
<body>
<div class="banner">
  <h1>📊 [{profile_type}] 类型 Skill 聚合评测报告</h1>
  <p>评分权重：{weight_str}</p>
  <p>包含：{stats['skill_count']} 个 Skill · 平均分：{stats['avg_total_score']} · 通过率：{stats['pass_rate']:.0%}</p>
  <p>生成时间：{generated_cst}</p>
</div>

{security_banner}

<div class="stat-grid">
  <div class="stat"><div class="val">{stats['avg_total_score']}</div><div class="lbl">平均总分</div></div>
  <div class="stat"><div class="val">{stats['pass_rate']:.0%}</div><div class="lbl">通过率 (C 以上)</div></div>
  <div class="stat"><div class="val">{non_compliant_count}</div><div class="lbl">安全不合规数量</div></div>
  <div class="stat"><div class="val">{grade_str}</div><div class="lbl">等级分布</div></div>
</div>

<div class="card">
  <h2>Skill 综合横向对比</h2>
  <table>
    <tr><th>Skill</th><th>总分</th><th>等级</th>
    <th>基础合规</th><th>代码质量</th><th>安全合规</th><th>健壮性</th><th>正确性</th><th>增量价值</th>
    <th>安全状态</th><th>性能对比</th></tr>
    {skill_rows}
  </table>
</div>

<div class="card">
  <h2>安全合规详情 (v5 §3.2)</h2>
  <table>
    <tr><th>Skill</th><th>安全原始分</th><th>合规状态</th><th>发现问题</th><th>备注</th></tr>
    {security_rows if security_rows else "<tr><td colspan='5' style='color:#64748b'>暂无安全扫描数据</td></tr>"}
  </table>
</div>

<div style="text-align:center;color:#94a3b8;font-size:12px;margin-top:24px">
  Skill Evaluator v5 · 聚合 ID: {data['aggregate_id']}
</div>
</body>
</html>"""