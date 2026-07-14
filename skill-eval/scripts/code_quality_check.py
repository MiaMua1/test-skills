#!/usr/bin/env python3
"""
Code quality check using Pylint and Radon
Part of Layer 2: Static Analysis
"""

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).parent))
from utils import run_command, parse_json_output


# pylint: disable=too-few-public-methods
class CodeQualityChecker:
    """Code quality checker for skills.

    Evaluates code quality across four dimensions:
    - PEP8 compliance (via Pylint)
    - Cyclomatic complexity (via Radon)
    - Code duplication
    - Type annotation coverage
    """

    def __init__(self, skill_path: str):
        """Initialize the code quality checker.

        Args:
            skill_path: Path to the skill directory to evaluate.
        """
        self.skill_path = Path(skill_path)
        self.results = {
            "layer": "2_code_quality",
            "score": 20,
            "max_score": 20,
            "checks": {},
            "issues": [],
            "metrics": {}
        }

    def _find_code_files(self) -> Dict:
        """Find all code files, excluding common build/cache directories.

        Returns:
            Dictionary mapping file extensions to lists of file paths.
        """
        excluded_dirs = {'node_modules', '.git', '__pycache__', '.venv', 'venv', 'dist', 'build'}
        exts = {'.py': [], '.js': [], '.ts': [], '.sh': [], '.rb': [], '.go': [], '.java': []}
        for ext, lst in exts.items():
            for f in self.skill_path.rglob(f'*{ext}'):
                parts = set(f.relative_to(self.skill_path).parts)
                if not parts.intersection(excluded_dirs):
                    lst.append(f)
        return exts

    def evaluate(self) -> Dict:
        """Run all code quality checks.

        Returns:
            Dictionary containing score, checks breakdown, issues, and metrics.
        """

        code_map = self._find_code_files()
        python_files = code_map['.py']
        all_code_files = [f for lst in code_map.values() for f in lst]

        if not all_code_files:
            # No code files at all — full score, mark as skipped
            self.results['score'] = 20
            self.results['skipped'] = True
            self.results['skip_reason'] = "No code files found in skill directory"
            self.results['metrics']['total_code_files'] = 0
            return self.results

        self.results['metrics']['total_code_files'] = len(all_code_files)
        self.results['metrics']['file_breakdown'] = {
            ext: len(lst) for ext, lst in code_map.items() if lst
        }

        if not python_files:
            # Has code but no Python — can't run Pylint/Radon, skip Python-specific checks
            self.results['checks']['pep8'] = {
                "score": 5, "max_score": 5, "skipped": True,
                "issues": ["No Python files — Pylint check skipped"]
            }
            self.results['checks']['complexity'] = {
                "score": 5, "max_score": 5, "skipped": True,
                "issues": ["No Python files — Radon complexity check skipped"]
            }
            self.results['checks']['duplication'] = {
                "score": 5, "max_score": 5, "skipped": True,
                "issues": ["No Python files — duplication check skipped"]
            }
            # Still check type annotations for JS/TS via heuristic
            type_penalty = self._check_type_annotations_js(code_map['.ts'], code_map['.js'])
            self.results['score'] = max(0, 20 - type_penalty)
            return self.results

        self.results['metrics']['python_files'] = len(python_files)

        # Check 1: PEP8 compliance (5 points max deduction)

        pep8_penalty = self._check_pep8(python_files)

        # Check 2: Cyclomatic complexity (5 points max deduction)
        complexity_penalty = self._check_complexity(python_files)

        # Check 3: Code duplication (5 points max deduction)
        duplication_penalty = self._check_duplication(python_files)

        # Check 4: Type annotations (5 points max deduction)
        type_penalty = self._check_type_annotations(python_files)

        # Calculate final score
        total_penalty = pep8_penalty + complexity_penalty + duplication_penalty + type_penalty
        self.results['score'] = max(0, 20 - total_penalty)

        return self.results

    def _check_pep8(self, python_files: List[Path]) -> float:
        """Check PEP8 compliance using pylint.

        Args:
            python_files: List of Python file paths to check.

        Returns:
            Penalty points (0-5) based on issues found.
        """
        penalty = 0
        issues = []

        try:
            # Check if pylint is available
            result = subprocess.run(
                ['pylint', '--version'],
                capture_output=True,
                timeout=5,
                check=False
            )
            if result.returncode != 0:
                # Pylint not available, skip
                self.results['checks']['pep8'] = {
                    "score": 5,
                    "max_score": 5,
                    "issues": ["pylint not available, skipped"],
                    "skipped": True
                }
                return 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.results['checks']['pep8'] = {
                "score": 5,
                "max_score": 5,
                "issues": ["pylint not available, skipped"],
                "skipped": True
            }
            return 0

        try:
            # Run pylint with JSON output
            # Use --rcfile to explicitly load project's pylintrc if it exists
            pylintrc_path = self.skill_path / ".pylintrc"
            pylint_cmd = ['pylint', '--output-format=json', '--reports=no']

            if pylintrc_path.exists():
                pylint_cmd.extend(['--rcfile', str(pylintrc_path)])

            file_args = [str(f) for f in python_files]

            result = subprocess.run(
                pylint_cmd + file_args,
                capture_output=True,
                timeout=60,
                text=True,
                cwd=str(self.skill_path),
                check=False
            )

            # Parse results
            if result.stdout:
                pylint_results = json.loads(result.stdout)

                # Count issues by type
                convention_count = 0
                refactor_count = 0
                warning_count = 0
                error_count = 0

                for issue in pylint_results:
                    issue_type = issue.get('type', '')

                    if issue_type == 'convention':
                        convention_count += 1
                    elif issue_type == 'refactor':
                        refactor_count += 1
                    elif issue_type == 'warning':
                        warning_count += 1
                    elif issue_type in ['error', 'fatal']:
                        error_count += 1

                    # Add to issues list (sample up to 10)
                    if len(issues) < 10:
                        issues.append({
                            "type": issue_type,
                            "message": issue.get('message', ''),
                            "file": issue.get('path', ''),
                            "line": issue.get('line', 0),
                            "symbol": issue.get('symbol', '')
                        })

                # Calculate penalty based on severity and count
                penalty = min(5, (
                    error_count * 0.5 +
                    warning_count * 0.2 +
                    refactor_count * 0.1 +
                    convention_count * 0.05
                ))

                self.results['metrics']['pylint'] = {
                    "errors": error_count,
                    "warnings": warning_count,
                    "refactor": refactor_count,
                    "conventions": convention_count,
                    "total_issues": len(pylint_results)
                }

        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Pylint check failed: {e}", file=sys.stderr)
            issues.append(f"Pylint check failed: {e}")

        self.results['checks']['pep8'] = {
            "score": max(0, 5 - penalty),
            "max_score": 5,
            "penalty": penalty,
            "issues": issues
        }

        return penalty

    def _check_complexity(self, python_files: List[Path]) -> float:
        """Check cyclomatic complexity using radon.

        Args:
            python_files: List of Python file paths to check.

        Returns:
            Penalty points (0-5) based on complexity grades.
        """
        penalty = 0
        issues = []

        try:
            # Check if radon is available
            result = subprocess.run(
                ['radon', '--version'],
                capture_output=True,
                timeout=5,
                check=False
            )
            if result.returncode != 0:
                self.results['checks']['complexity'] = {
                    "score": 5,
                    "max_score": 5,
                    "issues": ["radon not available, skipped"],
                    "skipped": True
                }
                return 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.results['checks']['complexity'] = {
                "score": 5,
                "max_score": 5,
                "issues": ["radon not available, skipped"],
                "skipped": True
            }
            return 0

        try:
            # Run radon cc (cyclomatic complexity)
            result = run_command(['radon', 'cc', '-j'] + list(python_files))

            radon_results = parse_json_output(result)
            if radon_results:

                # Count functions by complexity grade
                complexity_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 0}
                high_complexity_functions = []

                for file_path, functions in radon_results.items():
                    for func in functions:
                        grade = func.get('rank', 'A')
                        complexity_counts[grade] = complexity_counts.get(grade, 0) + 1

                        # Flag functions with complexity C or worse
                        if grade in ['C', 'D', 'E', 'F']:
                            high_complexity_functions.append({
                                "name": func.get('name', ''),
                                "complexity": func.get('complexity', 0),
                                "grade": grade,
                                "file": file_path,
                                "line": func.get('lineno', 0)
                            })

                # Calculate penalty
                # C=10-20, D=21-30, E=31-40, F=41+
                penalty = min(5, (
                    complexity_counts['F'] * 2.0 +
                    complexity_counts['E'] * 1.0 +
                    complexity_counts['D'] * 0.5 +
                    complexity_counts['C'] * 0.2
                ))

                # Add high complexity functions to issues
                for func in high_complexity_functions[:5]:  # Top 5
                    issues.append(
                        f"{func['name']} (grade {func['grade']}, complexity {func['complexity']}) "
                        f"at {func['file']}:{func['line']}"
                    )

                self.results['metrics']['complexity'] = {
                    "grade_counts": complexity_counts,
                    "high_complexity_count": len(high_complexity_functions),
                    "worst_functions": high_complexity_functions[:5]
                }

        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Radon check failed: {e}", file=sys.stderr)
            issues.append(f"Complexity check failed: {e}")

        self.results['checks']['complexity'] = {
            "score": max(0, 5 - penalty),
            "max_score": 5,
            "penalty": penalty,
            "issues": issues
        }

        return penalty

    def _check_duplication(self, python_files: List[Path]) -> float:
        """Check code duplication using radon raw metrics.

        Uses comment ratio as a proxy indicator for potential copy-paste code.

        Args:
            python_files: List of Python file paths to check.

        Returns:
            Penalty points (0-5) based on duplication indicators.
        """
        penalty = 0
        issues = []

        # Simple token-based duplication detection
        # More sophisticated: use tools like jscpd or radon's raw metrics

        try:
            # Use radon's raw metrics as a proxy for duplication
            result = subprocess.run(
                ['radon', 'raw', '-j'] + [str(f) for f in python_files],
                capture_output=True,
                timeout=30,
                text=True,
                check=False
            )

            if result.stdout:
                raw_results = json.loads(result.stdout)

                total_loc = 0
                total_comments = 0
                total_blanks = 0

                for _file_path, metrics in raw_results.items():
                    total_loc += metrics.get('loc', 0)
                    total_comments += metrics.get('comments', 0)
                    total_blanks += metrics.get('blank', 0)

                # If LOC is very small, skip duplication check
                if total_loc < 50:
                    self.results['checks']['duplication'] = {
                        "score": 5,
                        "max_score": 5,
                        "issues": ["Code too small for duplication analysis"],
                        "skipped": True
                    }
                    return 0

                # Calculate comment ratio (low comments might indicate copy-paste)
                comment_ratio = total_comments / max(1, total_loc)

                if comment_ratio < 0.1:  # Less than 10% comments
                    penalty += 1
                    msg = f"Low comment ratio ({comment_ratio:.1%}), may indicate copy-paste"
                    issues.append(msg)

                self.results['metrics']['code_metrics'] = {
                    "total_loc": total_loc,
                    "total_comments": total_comments,
                    "comment_ratio": comment_ratio
                }

        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            print(f"Warning: Duplication check limited: {e}", file=sys.stderr)

        self.results['checks']['duplication'] = {
            "score": max(0, 5 - penalty),
            "max_score": 5,
            "penalty": penalty,
            "issues": issues
        }

        return penalty

    def _check_type_annotations(self, python_files: List[Path]) -> float:
        """Check type annotation coverage in Python code.

        Analyzes AST to count functions with return type annotations or
        parameter annotations.

        Args:
            python_files: List of Python file paths to check.

        Returns:
            Penalty points (0-5) based on annotation coverage.
        """
        total_functions = 0
        annotated_functions = 0

        for py_file in python_files:
            total, annotated = self._count_annotations_in_file(py_file)
            total_functions += total
            annotated_functions += annotated

        # Calculate and apply penalty
        return self._calculate_annotation_penalty(total_functions, annotated_functions)

    def _count_annotations_in_file(self, py_file: Path) -> tuple:
        """Count function annotations in a single file.

        Args:
            py_file: Python file path to analyze.

        Returns:
            Tuple of (total_functions, annotated_functions).
        """
        total_functions = 0
        annotated_functions = 0

        try:
            content = py_file.read_text(encoding='utf-8')
            tree = ast.parse(content, filename=str(py_file))

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    total_functions += 1
                    if self._is_function_annotated(node):
                        annotated_functions += 1

        except (OSError, SyntaxError) as e:
            print(f"Warning: Could not parse {py_file}: {e}", file=sys.stderr)

        return total_functions, annotated_functions

    def _is_function_annotated(self, node: ast.FunctionDef) -> bool:
        """Check if a function has sufficient type annotations.

        Args:
            node: AST FunctionDef node to check.

        Returns:
            True if function has return type or majority of params annotated.
        """
        has_return_annotation = node.returns is not None

        params = [a for a in node.args.args if a.arg != 'self']
        total_params = len(params)

        if total_params == 0:
            return has_return_annotation

        param_annotations = sum(
            1 for arg in params if arg.annotation is not None
        )
        has_majority_params = param_annotations / total_params >= 0.5

        return has_return_annotation or has_majority_params

    def _calculate_annotation_penalty(self, total: int, annotated: int) -> float:
        """Calculate penalty based on annotation coverage.

        Args:
            total: Total number of functions.
            annotated: Number of annotated functions.

        Returns:
            Penalty points (0-5).
        """
        issues = []

        if total == 0:
            self.results['checks']['type_annotations'] = {
                "score": 5,
                "max_score": 5,
                "issues": ["No functions found"],
                "skipped": True
            }
            return 0

        coverage = annotated / total
        penalty = 0

        if coverage < 0.3:
            penalty = 5
            issues.append(f"Very low type annotation coverage ({coverage:.1%})")
        elif coverage < 0.5:
            penalty = 3
            issues.append(f"Low type annotation coverage ({coverage:.1%})")
        elif coverage < 0.7:
            penalty = 1
            issues.append(f"Moderate type annotation coverage ({coverage:.1%})")

        self.results['checks']['type_annotations'] = {
            "score": max(0, 5 - penalty),
            "max_score": 5,
            "penalty": penalty,
            "issues": issues
        }

        return penalty

    def _check_type_annotations_js(self, ts_files: list, js_files: list) -> float:
        """Heuristic type annotation check for TypeScript/JavaScript."""
        if ts_files:
            self.results['checks']['type_annotations'] = {
                "score": 5, "max_score": 5, "penalty": 0,
                "issues": [f"TypeScript files ({len(ts_files)}) — typed by nature"]
            }
            return 0
        if js_files:
            self.results['checks']['type_annotations'] = {
                "score": 3, "max_score": 5, "penalty": 2,
                "issues": ["Plain JS without TS — consider adding JSDoc or migrating"]
            }
            return 2
        self.results['checks']['type_annotations'] = {
            "score": 5, "max_score": 5, "skipped": True,
            "issues": ["Non-Python/JS code — type annotation check skipped"]
        }
        return 0


def main() -> None:
    """Command-line interface"""
    if len(sys.argv) < 2:
        print("Usage: python code_quality_check.py <skill-path>")
        sys.exit(1)

    skill_path = sys.argv[1]

    print(f"Running Code Quality Check on {skill_path}")
    print("=" * 70)

    checker = CodeQualityChecker(skill_path)
    results = checker.evaluate()

    # Print results
    print("\n[Layer 2] Code Quality Results")
    print(f"Score: {results['score']}/{results['max_score']}")

    # Print metrics
    if 'python_files' in results['metrics']:
        print("\nCode Metrics:")
        print(f"  Python files: {results['metrics']['python_files']}")

        if 'pylint' in results['metrics']:
            pylint_m = results['metrics']['pylint']
            print(f"  Pylint issues: {pylint_m['total_issues']} "
                  f"(E:{pylint_m['errors']} W:{pylint_m['warnings']} "
                  f"R:{pylint_m['refactor']} C:{pylint_m['conventions']})")

        if 'complexity' in results['metrics']:
            comp_m = results['metrics']['complexity']
            print(f"  High complexity functions: {comp_m['high_complexity_count']}")

        if 'type_annotations' in results['metrics']:
            type_m = results['metrics']['type_annotations']
            print(f"  Type annotation coverage: {type_m['coverage']:.1%} "
                  f"({type_m['annotated_functions']}/{type_m['total_functions']})")

    # Print check breakdown
    print("\nCheck Breakdown:")
    for check_name, check_data in results['checks'].items():
        print(f"  {check_name}: {check_data['score']}/{check_data['max_score']}")
        if check_data.get('issues'):
            for issue in check_data['issues'][:3]:  # Show first 3
                print(f"    ⚠️  {issue}")

    # Save results
    output_path = Path(skill_path) / "evaluation_results" / "code_quality_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")

    sys.exit(0)


if __name__ == "__main__":
    main()
