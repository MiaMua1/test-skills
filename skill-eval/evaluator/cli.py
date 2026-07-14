"""CLI entry point using click."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click


def _open_report(report_path: str) -> None:
    """Open the HTML report in the default browser."""
    import platform  # pylint: disable=import-outside-toplevel
    import subprocess  # pylint: disable=import-outside-toplevel

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", report_path])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", report_path])
        elif system == "Windows":
            subprocess.Popen(["start", report_path], shell=True)
    except OSError:
        pass  # silently skip if open command is unavailable


@click.group()
def main() -> None:
    """Skill Evaluator v2.0 — evaluate any AI Skill quality."""


@main.command()
@click.argument("skill_path")
@click.option("--mode", default="full", help="Evaluation mode: full|quick")
@click.option("--env", default="auto", help="Environment: auto|local|docker")
@click.option("--output-dir", default=None, help="Override storage base directory")
@click.option(
    "--evals-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Path to a local test-cases file (evals.json or a JSON array of test-case objects). "
        "When provided, skips auto-generation and uses these test cases directly."
    ),
)
@click.option(
    "--criteria-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Path to a local scoring-criteria file (scoring_criteria.json or a JSON array of "
        "per-tc criteria objects). When provided, uses these scoring rules instead of "
        "auto-generating them. profile_weight_snapshot is always re-stamped from live config."
    ),
)
@click.option(
    "--judge-model",
    default=None,
    help=(
        "LLM model used by the judge to score outputs "
        "(e.g. claude-opus-4-5, gpt-4o, glm-4). "
        "Overrides the JUDGE_MODEL env-var / .env value for this run only."
    ),
)
@click.option(
    "--eval-model",
    default=None,
    help=(
        "LLM model used to execute test cases (with-skill and without-skill runs). "
        "Overrides the EVAL_MODEL env-var / .env value for this run only."
    ),
)
@click.option(
    "--with-baseline",
    is_flag=True,
    default=False,
    help=(
        "Enable without-skill baseline comparison (delta / incremental-value scoring). "
        "Doubles token usage for L4 but measures how much value the skill adds over "
        "a plain LLM. Off by default to save cost."
    ),
)
@click.option(
    "--max-cases",
    default=None,
    type=int,
    help=(
        "Maximum number of test cases to generate in L3. "
        "When specified, overrides the default P0/P1/P2 counts and distributes "
        "the total across priorities (P0≥1, then P1, then P2). "
        "Example: --max-cases 3 generates ~2 P0 + 1 P1 + 0 P2."
    ),
)
def evaluate(
    skill_path: str,
    mode: str,
    env: str,
    output_dir: str | None,
    evals_file: str | None,
    criteria_file: str | None,
    judge_model: str | None,
    eval_model: str | None,
    with_baseline: bool,
    max_cases: int | None,
) -> None:
    """Evaluate a single skill at SKILL_PATH."""
    if output_dir:
        import os  # pylint: disable=import-outside-toplevel
        os.environ["STORAGE_BASE_DIR"] = output_dir

    from evaluator.pipeline import EvaluationPipeline  # pylint: disable=import-outside-toplevel

    pipeline = EvaluationPipeline(
        skill_path,
        mode=mode,
        env_type=env,
        evals_file=evals_file,
        criteria_file=criteria_file,
        judge_model=judge_model,
        eval_model=eval_model,
        with_baseline=with_baseline,
        max_cases=max_cases,
    )
    result = asyncio.run(pipeline.evaluate())

    click.echo(json.dumps(result, ensure_ascii=False, indent=2))

    verdict = result.get("verdict", "FAILED")
    click.echo(f"\n{'✅' if verdict not in ('BLOCKED', 'FAILED') else '❌'} "
               f"{result.get('skill_name')} — Score: {result.get('total_score')}/100 "
               f"Grade: {result.get('grade')} ({verdict})")

    if result.get("report_path"):
        click.echo(f"📊 Report: {result['report_path']}")
        _open_report(result["report_path"])

    # Hint: suggest baseline comparison if not already enabled
    if not with_baseline and verdict not in ("BLOCKED", "FAILED"):
        click.echo(
            "\n💡 Tip: 本次评测未包含增量价值（without-skill 基线对比）。"
            "\n   添加 --with-baseline 可衡量 skill 相对于纯 LLM 的增量价值，"
            "但会增加约 1 倍的 token 消耗。"
        )

    sys.exit(0 if verdict not in ("BLOCKED", "FAILED") else 1)

@main.command()
@click.argument("skill_path")
@click.argument("eval_id")
@click.option("--output-dir", default=None, help="Override storage base directory")
@click.option("--judge-model", default=None, help="LLM model for judge scoring")
@click.option("--eval-model", default=None, help="LLM model for test case execution")
@click.option(
    "--retry-failed",
    is_flag=True,
    default=False,
    help=(
        "Also re-run test cases that completed but scored correct_raw=0. "
        "Useful when a previous run had transient errors (e.g. API auth failures) "
        "that were not detected as failures."
    ),
)
@click.option(
    "--with-baseline",
    is_flag=True,
    default=False,
    help=(
        "Enable without-skill baseline comparison (delta module). "
        "When resuming, this will run without-skill for completed test cases "
        "that are missing baseline results, without re-running with-skill."
    ),
)
def resume(skill_path: str, eval_id: str, output_dir: str | None, judge_model: str | None, eval_model: str | None, retry_failed: bool, with_baseline: bool) -> None:
    """Resume an interrupted evaluation from L4 checkpoint.

    SKILL_PATH is the skill directory. EVAL_ID is the evaluation to resume.
    Skips L1-L3, loads existing evals/criteria, re-runs only incomplete test cases.
    Use --retry-failed to also re-run cases that scored 0.
    Use --with-baseline to add without-skill baseline comparison.
    """
    if output_dir:
        import os
        os.environ["STORAGE_BASE_DIR"] = output_dir

    from evaluator.pipeline import EvaluationPipeline

    pipeline = EvaluationPipeline(
        skill_path,
        judge_model=judge_model,
        eval_model=eval_model,
        with_baseline=with_baseline,
    )
    result = asyncio.run(pipeline.resume(eval_id, retry_failed=retry_failed))

    click.echo(json.dumps(result, ensure_ascii=False, indent=2))

    verdict = result.get("verdict", "FAILED")
    click.echo(f"\n{'✅' if verdict not in ('BLOCKED', 'FAILED') else '❌'} "
               f"{result.get('skill_name')} — Score: {result.get('total_score')}/100 "
               f"Grade: {result.get('grade')} ({verdict})")

    if result.get("report_path"):
        click.echo(f"📊 Report: {result['report_path']}")
        _open_report(result["report_path"])

    sys.exit(0 if verdict not in ("BLOCKED", "FAILED") else 1)

@main.command(name="regenerate-report")
@click.argument("skill_path")
@click.argument("eval_id")
@click.option("--output-dir", default=None, help="Override storage base directory")
def regenerate_report(skill_path: str, eval_id: str, output_dir: str | None) -> None:
    """Regenerate HTML report from existing eval results.

    SKILL_PATH is the skill directory. EVAL_ID is the evaluation to regenerate.
    Reads existing L4 results and scoring data, then re-runs L5 report generation only.
    """
    if output_dir:
        import os
        os.environ["STORAGE_BASE_DIR"] = output_dir

    from evaluator.pipeline import EvaluationPipeline

    pipeline = EvaluationPipeline(skill_path)
    result = asyncio.run(pipeline.regenerate_report(eval_id))

    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("report_path"):
        click.echo(f"📊 Report regenerated: {result['report_path']}")

@main.command()
@click.argument("eval_ids", nargs=-1, required=True)
@click.option("--storage-base", default="./storage")
def aggregate(eval_ids: tuple[str, ...], storage_base: str) -> None:
    """Generate an aggregate report for multiple EVAL_IDS."""
    from evaluator.layers.layer6_aggregate import Layer6Aggregate  # pylint: disable=import-outside-toplevel

    agg = Layer6Aggregate(Path(storage_base))
    result = asyncio.run(agg.run(list(eval_ids)))
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    click.echo(f"\n📊 Aggregate report: {result.get('aggregate_report_path')}")


if __name__ == "__main__":
    main()