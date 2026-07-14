#!/usr/bin/env python3
"""
Batch Skill Evaluator
Evaluate multiple skills in sequence, generate per-skill HTML reports,
and produce an aggregate comparison report.

Usage:
    python batch_evaluate.py skill1/ skill2/ skill3/
    python batch_evaluate.py --file skills.txt
    python batch_evaluate.py https://github.com/user/my-skill local/skill
    python batch_evaluate.py --mode quick skill1/ skill2/
    python batch_evaluate.py --output-dir ./batch_results/ skill1/ skill2/
"""

import html as html_module
import json
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_single_evaluation(skill_path: str, mode: str = "full") -> Dict:
    """Run evaluation for a single skill and return the eval_data dict."""
    runner = Path(__file__).parent / "run_evaluation.py"
    result = subprocess.run(
        [sys.executable, str(runner), skill_path, f"--mode={mode}"],
        capture_output=True, text=True, timeout=300, check=False
    )

    # Find evaluation_results directory for the skill
    if not skill_path.startswith("http"):
        eval_data_path = Path(skill_path) / "evaluation_results" / "eval_data.json"
        if eval_data_path.exists():
            try:
                with open(eval_data_path, encoding="utf-8") as f:
                    data = json.load(f)
                data["_eval_results_dir"] = str(eval_data_path.parent)
                return data
            except json.JSONDecodeError:
                pass

    return {
        "skill_name": Path(skill_path).name or skill_path,
        "eval_profile": "unknown",
        "summary": {
            "total_score": 0, "max_score": 100, "grade": "F",
            "status": "error",
            "blocking_reason": result.stderr[:300] if result.returncode != 0 else "未能读取结果",
        },
        "score_breakdown": [],
        "layers": {},
        "test_cases": [],
        "bugs": [],
        "recommendations": [],
        "key_findings": {"strengths": [], "issues": []},
        "effect_validation": None,
        "_error": result.stderr[:500],
    }


def load_skills_from_file(filepath: str) -> List[str]:
    """Load skill paths from a text file (one per line, # comments ignored)."""
    paths = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)
    return paths


# ---------------------------------------------------------------------------
# Aggregate comparison HTML report
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return html_module.escape(str(text))


def _score_color(pct: float) -> str:
    if pct >= 90:
        return "#22c55e"
    if pct >= 70:
        return "#f59e0b"
    if pct >= 50:
        return "#f97316"
    return "#ef4444"


def _grade_badge(grade: str) -> str:
    colors = {
        "A": ("#dcfce7", "#16a34a"),
        "B": ("#dbeafe", "#2563eb"),
        "C": ("#fef9c3", "#ca8a04"),
        "D": ("#ffedd5", "#ea580c"),
        "F": ("#fee2e2", "#dc2626"),
    }
    bg, fg = colors.get(grade, ("#f3f4f6", "#6b7280"))
    return (f'<span style="font-size:0.75rem;font-weight:700;padding:0.15rem 0.5rem;'
            f'border-radius:4px;background:{bg};color:{fg};">{_esc(grade)}</span>')


def build_aggregate_html(results: List[Dict], batch_meta: Dict) -> str:
    """Generate a standalone HTML aggregate comparison report."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(results)
    passed = sum(1 for r in results
                 if (r.get("summary", {}).get("status") or "") not in ("error", "blocked"))
    avg = batch_meta.get("average_score", 0)

    # Sort by score desc
    ranked = sorted(results, key=lambda r: r.get("summary", {}).get("total_score", 0), reverse=True)

    # Build rows
    rows_html = ""
    for rank, r in enumerate(ranked, 1):
        sm = r.get("summary", {})
        score = sm.get("total_score", 0)
        grade = sm.get("grade", "F")
        status = sm.get("status", "?")
        name = r.get("skill_name", "?")
        profile = r.get("eval_profile", "?")
        pct = round(score / sm.get("max_score", 100) * 100)
        col = _score_color(pct)
        blocking = sm.get("blocking_reason", "") or ""

        # score breakdown mini bars
        breakdown = r.get("score_breakdown", [])
        mini_bars = ""
        for bd in breakdown:
            if bd.get("max", 0) > 0:
                bp = min(100, round(bd["score"] / bd["max"] * 100))
                bc = _score_color(bp)
                mini_bars += (
                    f'<div title="{_esc(bd["label"])}: {bd["score"]}/{bd["max"]}" '
                    f'style="display:inline-block;width:36px;height:6px;border-radius:3px;'
                    f'background:linear-gradient(to right,{bc} {bp}%,#e5e7eb {bp}%);'
                    f'margin:1px;"></div>'
                )

        # Key strengths / issues
        strengths = (r.get("key_findings") or {}).get("strengths", [])
        issues = (r.get("key_findings") or {}).get("issues", [])
        kf_html = ""
        for s in strengths[:2]:
            kf_html += f'<div style="font-size:0.7rem;color:#16a34a;">✅ {_esc(s)}</div>'
        for i in issues[:2]:
            kf_html += f'<div style="font-size:0.7rem;color:#dc2626;">⚠️ {_esc(i)}</div>'

        # Per-skill report link
        results_dir = r.get("_eval_results_dir", "")
        report_link = ""
        if results_dir:
            report_path = Path(results_dir) / "report.html"
            if report_path.exists():
                report_link = (
                    f'<a href="file://{report_path}" target="_blank" '
                    f'style="font-size:0.72rem;color:#3b82f6;text-decoration:none;">'
                    f'查看详细报告 →</a>'
                )

        status_badge = ""
        if status in ("blocked", "error"):
            status_badge = (
                '<span style="font-size:0.68rem;background:#fee2e2;color:#dc2626;'
                'padding:0.1rem 0.35rem;border-radius:3px;margin-left:0.3rem;">'
                'BLOCKED</span>'
            )
        if blocking:
            status_badge += (
                '<div style="font-size:0.68rem;color:#dc2626;margin-top:0.2rem;">'
                f'{_esc(blocking[:80])}</div>'
            )

        rows_html += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:0.875rem 0.75rem;font-weight:700;color:#6b7280;font-size:0.9rem;">#{rank}</td>
          <td style="padding:0.875rem 0.75rem;">
            <div style="font-weight:600;font-size:0.9rem;">{_esc(name)}</div>
            <div style="font-size:0.7rem;color:#9ca3af;margin-top:0.1rem;">{_esc(profile)}</div>
            {status_badge}
          </td>
          <td style="padding:0.875rem 0.75rem;text-align:center;">
            <div style="font-family:'Poppins',sans-serif;font-size:1.4rem;font-weight:700;color:{col};">{score}</div>
            <div style="font-size:0.68rem;color:#9ca3af;">/ 100</div>
          </td>
          <td style="padding:0.875rem 0.75rem;text-align:center;">{_grade_badge(grade)}</td>
          <td style="padding:0.875rem 0.75rem;">{mini_bars}</td>
          <td style="padding:0.875rem 0.75rem;">{kf_html}</td>
          <td style="padding:0.875rem 0.75rem;">{report_link}</td>
        </tr>"""

    # Dimension comparison table header
    all_labels = []
    seen_labels = set()
    for r in results:
        for bd in r.get("score_breakdown", []):
            if bd.get("max", 0) > 0 and bd["label"] not in seen_labels:
                seen_labels.add(bd["label"])
                all_labels.append(bd["label"])

    dim_header = "".join(f"<th style='text-align:center;padding:0.5rem;font-size:0.75rem;'>{_esc(l)}</th>" for l in all_labels)
    dim_rows = ""
    for r in ranked:
        scores_by_label = {bd["label"]: bd for bd in r.get("score_breakdown", [])}
        dim_rows += f"<tr style='border-bottom:1px solid #e5e7eb;'><td style='padding:0.5rem;font-weight:600;font-size:0.8rem;'>{_esc(r.get('skill_name','?'))}</td>"
        for label in all_labels:
            bd = scores_by_label.get(label, {})
            if bd.get("max", 0) > 0:
                sp = min(100, round(bd["score"] / bd["max"] * 100))
                sc = _score_color(sp)
                dim_rows += f"<td style='text-align:center;padding:0.5rem;font-size:0.8rem;font-weight:600;color:{sc};'>{bd['score']}/{bd['max']}</td>"
            else:
                dim_rows += "<td style='text-align:center;padding:0.5rem;color:#9ca3af;font-size:0.75rem;'>—</td>"
        dim_rows += "</tr>"

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>批量评测聚合报告 · {ts}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', sans-serif; background: #f8fafc; color: #1e293b; }}
  .page {{ max-width: 1200px; margin: 0 auto; padding: 2rem 1.5rem; }}
  .header {{ margin-bottom: 2rem; }}
  h1 {{ font-family: 'Poppins', sans-serif; font-size: 1.5rem; font-weight: 700; }}
  .sub {{ color: #64748b; font-size: 0.875rem; margin-top: 0.25rem; }}
  .stat-bar {{ display: flex; gap: 1.5rem; margin: 1.5rem 0; flex-wrap: wrap; }}
  .stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1rem 1.5rem; flex: 1; min-width: 160px; }}
  .stat-val {{ font-family: 'Poppins', sans-serif; font-size: 2rem; font-weight: 700; }}
  .stat-lbl {{ font-size: 0.75rem; color: #64748b; margin-top: 0.15rem; }}
  .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; margin-bottom: 1.5rem; }}
  .card-head {{ padding: 1rem 1.25rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: 0.9rem; background: #f8fafc; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 0.625rem 0.75rem; font-size: 0.78rem; font-weight: 600; color: #64748b; background: #f8fafc; border-bottom: 1px solid #e2e8f0; }}
  tr:hover {{ background: #f8fafc; }}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <h1>📊 批量评测聚合报告</h1>
    <div class="sub">生成时间：{ts} · 共 {total} 个 Skill</div>
  </div>

  <div class="stat-bar">
    <div class="stat">
      <div class="stat-val" style="color:{_score_color(avg)};">{avg}</div>
      <div class="stat-lbl">平均分 / 100</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#22c55e;">{passed}</div>
      <div class="stat-lbl">通过 / {total}</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#ef4444;">{total - passed}</div>
      <div class="stat-lbl">阻断</div>
    </div>
    <div class="stat">
      <div class="stat-val">{total}</div>
      <div class="stat-lbl">总计评测</div>
    </div>
  </div>

  <div class="card">
    <div class="card-head">排名对比</div>
    <table>
      <thead>
        <tr>
          <th style="width:48px;">排名</th>
          <th>Skill 名称</th>
          <th style="text-align:center;width:80px;">总分</th>
          <th style="text-align:center;width:60px;">等级</th>
          <th>各维度</th>
          <th>主要发现</th>
          <th>报告</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-head">各维度分数对比</div>
    <table>
      <thead><tr><th>Skill</th>{dim_header}</tr></thead>
      <tbody>{dim_rows}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_comparison_report(results: List[Dict]) -> Dict:
    """Build a structured comparison report from per-skill results."""
    sorted_results = sorted(results, key=lambda r: r.get("summary", {}).get("total_score", 0), reverse=True)

    ranking = []
    for rank, r in enumerate(sorted_results, start=1):
        sm = r.get("summary", {})
        name = r.get("skill_name", Path(r.get("_skill_path", "unknown")).name)
        ranking.append({
            "rank": rank,
            "skill": name,
            "score": sm.get("total_score", 0),
            "grade": sm.get("grade", "F"),
            "status": sm.get("status", "unknown"),
            "profile": r.get("eval_profile", "?"),
            "blocking_reason": sm.get("blocking_reason"),
        })

    passed = sum(1 for r in results
                 if r.get("summary", {}).get("status") not in ("blocked", "error"))
    avg_score = (
        sum(r.get("summary", {}).get("total_score", 0) for r in results) / len(results)
        if results else 0
    )

    return {
        "batch_evaluation": {
            "generated_at": datetime.now().isoformat(),
            "total_skills": len(results),
            "passed": passed,
            "blocked": len(results) - passed,
            "average_score": round(avg_score, 1),
        },
        "ranking": ranking,
        "details": results,
    }


def print_comparison_table(report: Dict) -> None:
    """Print a formatted comparison table to stdout."""
    batch = report["batch_evaluation"]
    ranking = report["ranking"]

    print("\n" + "=" * 75)
    print("BATCH EVALUATION SUMMARY")
    print("=" * 75)
    print(f"Total: {batch['total_skills']}  |  Passed: {batch['passed']}  |  "
          f"Blocked: {batch['blocked']}  |  Avg: {batch['average_score']}/100")
    print()
    print(f"{'Rank':<5} {'Skill':<30} {'Profile':<16} {'Score':<8} {'Grade':<7} Status")
    print("-" * 75)
    for r in ranking:
        blocked_note = f" [{r['blocking_reason'][:40]}]" if r.get("blocking_reason") else ""
        print(
            f"{r['rank']:<5} {r['skill']:<30} {r['profile']:<16} "
            f"{r['score']:<8} {r['grade']:<7} {r['status']}{blocked_note}"
        )
    print("=" * 75)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments and run batch evaluation."""
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(1)

    skill_paths: List[str] = []
    mode = "full"
    output_file: Optional[str] = None
    output_dir: Optional[str] = None
    i = 0
    while i < len(args):
        if args[i] == "--file" and i + 1 < len(args):
            skill_paths.extend(load_skills_from_file(args[i + 1]))
            i += 2
        elif args[i].startswith("--mode="):
            mode = args[i].split("=", 1)[1]; i += 1
        elif args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]; i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_file = args[i + 1]; i += 2
        elif args[i].startswith("--output="):
            output_file = args[i].split("=", 1)[1]; i += 1
        elif args[i].startswith("--output-dir="):
            output_dir = args[i].split("=", 1)[1]; i += 1
        elif args[i] == "--output-dir" and i + 1 < len(args):
            output_dir = args[i + 1]; i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            i += 2  # consume and ignore (label is derived from output-dir name)
        elif args[i].startswith("--label="):
            i += 1  # consume and ignore
        elif args[i].startswith("--"):
            i += 1  # skip unknown flags
        else:
            skill_paths.append(args[i]); i += 1

    if not skill_paths:
        print("❌ No skill paths provided.")
        sys.exit(1)

    print(f"\n{'='*75}")
    print(f"BATCH SKILL EVALUATION  ({len(skill_paths)} skills, mode={mode})")
    print(f"{'='*75}")

    results = []
    for idx, skill_path in enumerate(skill_paths, start=1):
        print(f"\n[{idx}/{len(skill_paths)}] ▸ {skill_path}")
        try:
            result = run_single_evaluation(skill_path, mode=mode)
        except (OSError, subprocess.TimeoutExpired) as e:
            result = {
                "skill_name": Path(skill_path).name,
                "eval_profile": "unknown",
                "summary": {
                    "total_score": 0, "max_score": 100, "grade": "F",
                    "status": "error",
                },
                "_error": str(e),
            }
        result["_skill_path"] = skill_path
        results.append(result)

        sm = result.get("summary", {})
        print(f"   Score: {sm.get('total_score',0)}/100  Grade: {sm.get('grade','?')}"
              f"  Status: {sm.get('status','?')}")

    report = build_comparison_report(results)
    print_comparison_table(report)

    # Save JSON
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if not output_file:
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            output_file = str(Path(output_dir) / f"batch_eval_{ts}.json")
        else:
            output_file = f"batch_eval_{ts}.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n💾 JSON results: {output_file}")

    # Generate aggregate HTML report
    batch_meta = report["batch_evaluation"]
    agg_html = build_aggregate_html(results, batch_meta)
    agg_path = Path(output_file).with_suffix("") / "index.html"
    agg_path = Path(str(output_file).replace(".json", "_aggregate.html"))
    with open(agg_path, "w", encoding="utf-8") as f:
        f.write(agg_html)
    print(f"📊 Aggregate report: {agg_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())