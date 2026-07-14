"""Skill entry point — wraps the evaluation pipeline as a single Skill tool.

Claude calls this module after reading SKILL.md. It parses the user's request,
runs the pipeline, and returns a structured result synchronously.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def run_evaluation(skill_path: str, mode: str = "full", env: str = "auto") -> dict:
    """Execute a full skill evaluation synchronously.

    Args:
        skill_path: Absolute path to the skill directory or GitHub URL.
        mode: "full" | "quick" | "custom"
        env: "auto" | "docker" | "local"

    Returns:
        Structured evaluation result dict (see SKILL.md § Returns).
    """
    from evaluator.pipeline import EvaluationPipeline  # pylint: disable=import-outside-toplevel

    pipeline = EvaluationPipeline(skill_path, mode=mode, env_type=env)
    return asyncio.run(pipeline.evaluate())


def run_aggregate(eval_ids: list[str], storage_base: str = "./storage") -> dict:
    """Generate an aggregate report for multiple eval_ids.

    Args:
        eval_ids: List of eval_id strings (≥2 required).
        storage_base: Path to the storage directory.

    Returns:
        Dict with aggregate_data_path and aggregate_report_path.
    """
    from evaluator.layers.layer6_aggregate import Layer6Aggregate  # pylint: disable=import-outside-toplevel

    agg = Layer6Aggregate(Path(storage_base))
    return asyncio.run(agg.run(eval_ids))


if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        print("Usage: python skill_entry.py <skill_path> [--mode full|quick] [--env auto|local|docker]")
        sys.exit(1)

    skill_path_arg = sys.argv[1]
    mode_arg = "full"
    env_arg = "auto"

    for arg in sys.argv[2:]:
        if arg.startswith("--mode="):
            mode_arg = arg.split("=", 1)[1]
        elif arg.startswith("--env="):
            env_arg = arg.split("=", 1)[1]

    result = run_evaluation(skill_path_arg, mode=mode_arg, env=env_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("verdict") not in ("BLOCKED", "FAILED") else 1)
