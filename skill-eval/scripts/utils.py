"""Shared utility functions for skill evaluator."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# pylint: disable=wrong-import-position


def run_command(
    cmd: List[Union[str, Path]],
    timeout: int = 30,
    check: bool = False,
    cwd: Optional[Path] = None
) -> subprocess.CompletedProcess:
    """Run a shell command and return the result.

    Args:
        cmd: Command and arguments to run.
        timeout: Timeout in seconds.
        check: Whether to raise an exception on non-zero exit.
        cwd: Working directory for the command.

    Returns:
        CompletedProcess with stdout, stderr, and returncode.
    """
    str_cmd = [str(c) for c in cmd]
    return subprocess.run(
        str_cmd,
        capture_output=True,
        timeout=timeout,
        text=True,
        check=check,
        cwd=cwd
    )


def parse_json_output(result: subprocess.CompletedProcess) -> Optional[Any]:
    """Parse JSON from command stdout if available.

    Args:
        result: CompletedProcess from run_command.

    Returns:
        Parsed JSON data or None if no output.
    """
    if result.stdout:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
    return None


def calculate_grade(score: float) -> str:
    """Calculate letter grade from numerical score.

    Args:
        score: Numerical score (0-100).

    Returns:
        Letter grade: A (90+), B (80-89), C (70-79), D (60-69), or F (<60).
    """
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def save_json_results(results: Dict, output_path: Path, passed: bool) -> None:
    """Save results to JSON file and exit with appropriate code.

    Args:
        results: Evaluation results dictionary.
        output_path: Path to save the JSON file.
        passed: Whether the evaluation passed.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")
    sys.exit(0 if passed else 1)
