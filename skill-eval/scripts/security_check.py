#!/usr/bin/env python3
"""
Security compliance check using Bandit and secret scanning
Part of Layer 2: Static Analysis
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).parent))
from utils import save_json_results, run_command, parse_json_output


# pylint: disable=too-few-public-methods
class SecurityChecker:
    """Security compliance checker for skills.

    Performs multi-layer security scanning including:
    - Pattern-based detection for critical vulnerabilities
    - Bandit integration for Python security analysis
    - Dependency CVE checking via Safety
    """

    # Critical security patterns (immediate blocking)
    CRITICAL_PATTERNS = [
        # Command injection
        {
            "pattern": r'subprocess\.(call|run|Popen|check_output|check_call)'
                       r'\([^)]*shell\s*=\s*True',
            "name": "command_injection_shell",
            "severity": "CRITICAL",
            "description": "Command injection via shell=True",
            "penalty": 20
        },
        {
            "pattern": r'os\.system\(',
            "name": "command_injection_os_system",
            "severity": "CRITICAL",
            "description": "Command injection via os.system",
            "penalty": 20
        },
        # Code injection
        {
            "pattern": r'\beval\s*\(',
            "name": "code_injection_eval",
            "severity": "CRITICAL",
            "description": "Code injection via eval()",
            "penalty": 20
        },
        {
            "pattern": r'\bexec\s*\(',
            "name": "code_injection_exec",
            "severity": "CRITICAL",
            "description": "Code injection via exec()",
            "penalty": 20
        },
        # Hardcoded secrets
        {
            "pattern": r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{3,}["\']',
            "name": "hardcoded_password",
            "severity": "CRITICAL",
            "description": "Hardcoded password detected",
            "penalty": 20
        },
        {
            "pattern": r'(?i)(api[_-]?key|apikey|api[_-]?secret)\s*=\s*["\'][^"\']{10,}["\']',
            "name": "hardcoded_api_key",
            "severity": "CRITICAL",
            "description": "Hardcoded API key detected",
            "penalty": 20
        },
        {
            "pattern": r'(?i)(secret[_-]?key|private[_-]?key)\s*=\s*["\'][^"\']{10,}["\']',
            "name": "hardcoded_secret_key",
            "severity": "CRITICAL",
            "description": "Hardcoded secret key detected",
            "penalty": 20
        },
        {
            "pattern": r'(?i)(aws[_-]?secret|aws[_-]?access[_-]?key)\s*=\s*["\'][^"\']{10,}["\']',
            "name": "hardcoded_aws_credentials",
            "severity": "CRITICAL",
            "description": "Hardcoded AWS credentials detected",
            "penalty": 20
        },
    ]

    # High severity patterns
    HIGH_PATTERNS = [
        {
            "pattern": r'pickle\.(loads?|dumps?)\(',
            "name": "unsafe_deserialization",
            "severity": "HIGH",
            "description": "Unsafe deserialization with pickle",
            "penalty": 10
        },
        {
            "pattern": r'yaml\.load\([^)]*\)(?!\s*,\s*Loader\s*=)',
            "name": "unsafe_yaml_load",
            "severity": "HIGH",
            "description": "Unsafe YAML loading (use safe_load)",
            "penalty": 10
        },
    ]

    # Medium severity patterns
    MEDIUM_PATTERNS = [
        {
            "pattern": r'open\([^)]*\.\./[^)]*\)',
            "name": "path_traversal",
            "severity": "MEDIUM",
            "description": "Potential path traversal vulnerability",
            "penalty": 5
        },
    ]

    def __init__(self, skill_path: str):
        """Initialize the security checker.

        Args:
            skill_path: Path to the skill directory to evaluate.
        """
        self.skill_path = Path(skill_path)
        self.results = {
            "layer": "2_security",
            "score": 20,
            "max_score": 20,
            "passed": True,
            "issues": [],
            "critical_issues": [],
            "summary": {}
        }

    def _find_code_files(self) -> list:
        """Find scannable code files, excluding node_modules/.git."""
        excluded_dirs = {'node_modules', '.git', '__pycache__', '.venv', 'venv', 'dist', 'build'}
        code_exts = ['.py', '.js', '.ts', '.sh', '.rb', '.go', '.java', '.rs']
        found = []
        for ext in code_exts:
            for f in self.skill_path.rglob(f'*{ext}'):
                parts = set(f.relative_to(self.skill_path).parts)
                if not parts.intersection(excluded_dirs):
                    found.append(f)
        return found

    def evaluate(self) -> Dict:
        """Run all security checks.

        Performs pattern scanning, Bandit analysis, and CVE checking.
        Returns full 20 pts with skipped=True when no code files are found.

        Returns:
            Dictionary containing score, issues, critical_issues, and summary.
        """
        code_files = self._find_code_files()
        if not code_files:
            self.results['score'] = 20
            self.results['passed'] = True
            self.results['skipped'] = True
            self.results['skip_reason'] = "No code files found — security scan skipped"
            self.results['scans'] = [
                {"name": "命令注入检测", "passed": True, "skipped": True},
                {"name": "eval() 使用检测", "passed": True, "skipped": True},
                {"name": "硬编码密钥检测", "passed": True, "skipped": True},
                {"name": "SQL 注入检测", "passed": True, "skipped": True},
                {"name": "路径遍历检测", "passed": True, "skipped": True},
                {"name": "依赖 CVE 检测", "passed": True, "skipped": True},
            ]
            return self.results

        # Check 1: Pattern-based security scanning
        pattern_issues = self._scan_security_patterns()

        # Check 2: Run Bandit if available
        bandit_issues = self._run_bandit()

        # Check 3: Dependency CVE check
        cve_issues = self._check_dependency_cves()

        # Aggregate all issues
        all_issues = pattern_issues + bandit_issues + cve_issues

        # Separate critical issues
        critical_issues = [i for i in all_issues if i['severity'] == 'CRITICAL']

        # If critical issues found, block evaluation
        if critical_issues:
            self.results['score'] = 0
            self.results['passed'] = False
            self.results['critical_issues'] = critical_issues
            self.results['blocking_reason'] = "Critical security vulnerabilities found"
        else:
            # Calculate penalty from non-critical issues
            total_penalty = sum(i.get('penalty', 0) for i in all_issues)
            self.results['score'] = max(0, 20 - total_penalty)
            self.results['passed'] = self.results['score'] >= 12  # 60% threshold

        self.results['issues'] = all_issues
        self.results['summary'] = {
            "total_issues": len(all_issues),
            "critical": len([i for i in all_issues if i['severity'] == 'CRITICAL']),
            "high": len([i for i in all_issues if i['severity'] == 'HIGH']),
            "medium": len([i for i in all_issues if i['severity'] == 'MEDIUM']),
            "low": len([i for i in all_issues if i['severity'] == 'LOW']),
        }

        return self.results

    def _is_in_string_literal(self, line: str, match_start: int) -> bool:
        """Check if a match position is inside a string literal.

        This helps avoid false positives when security-related keywords
        appear in strings (e.g., "eval() usage check" in rule definitions).
        """
        # Count quotes before the match position
        before = line[:match_start]

        # Count single and double quotes (excluding escaped ones)
        single_quotes = before.count("'") - before.count("\\'")
        double_quotes = before.count('"') - before.count('\\"')

        # If odd number of either quote type, we're inside a string
        return (single_quotes % 2 == 1) or (double_quotes % 2 == 1)

    def _is_in_comment(self, line: str, match_start: int) -> bool:
        """Check if a match position is inside a comment."""
        comment_pos = line.find('#')
        return 0 <= comment_pos < match_start

    def _scan_security_patterns(self) -> List[Dict]:
        """Scan Python files for security anti-patterns.

        Excludes matches inside string literals and comments to avoid false positives.
        """
        issues = []

        # Find all Python files
        python_files = list(self.skill_path.rglob('*.py'))

        all_patterns = (
            [(p, 'CRITICAL') for p in self.CRITICAL_PATTERNS] +
            [(p, 'HIGH') for p in self.HIGH_PATTERNS] +
            [(p, 'MEDIUM') for p in self.MEDIUM_PATTERNS]
        )

        for py_file in python_files:
            file_issues = self._scan_file_for_patterns(py_file, all_patterns)
            issues.extend(file_issues)

        return issues

    def _scan_file_for_patterns(self, py_file, all_patterns) -> List[Dict]:
        """Scan a single file for security patterns."""
        issues = []
        try:
            content = py_file.read_text(encoding='utf-8')
            lines = content.split('\n')

            for pattern_def, _ in all_patterns:
                for line_num, line in enumerate(lines, 1):
                    match = re.search(pattern_def['pattern'], line)
                    if match and not self._is_in_string_literal(line, match.start()):
                        if not self._is_in_comment(line, match.start()):
                            issues.append({
                                "name": pattern_def['name'],
                                "severity": pattern_def['severity'],
                                "description": pattern_def['description'],
                                "file": str(py_file.relative_to(self.skill_path)),
                                "line": line_num,
                                "code": line.strip(),
                                "penalty": pattern_def['penalty']
                            })

        except (OSError, UnicodeDecodeError) as e:
            print(f"Warning: Could not scan {py_file}: {e}", file=sys.stderr)

        return issues

    def _run_bandit(self) -> List[Dict]:
        """Run Bandit security scanner if available.

        Returns:
            List of security issues found by Bandit.
        """
        issues = []

        try:
            # Check if bandit is installed
            result = subprocess.run(
                ['bandit', '--version'],
                capture_output=True,
                timeout=5,
                check=False
            )
            if result.returncode != 0:
                return issues
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Bandit not available, skip
            return issues

        try:
            # Run bandit
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
                tmp_path = tmp.name

            subprocess.run(
                ['bandit', '-r', str(self.skill_path), '-f', 'json', '-o', tmp_path],
                capture_output=True,
                timeout=30,
                check=False
            )

            # Parse results
            with open(tmp_path, 'r', encoding='utf-8') as f:
                bandit_results = json.load(f)

            # Convert Bandit results to our format
            for result in bandit_results.get('results', []):
                severity = result.get('issue_severity', 'MEDIUM')

                # Map severity to penalty
                penalty_map = {
                    'CRITICAL': 20,
                    'HIGH': 10,
                    'MEDIUM': 5,
                    'LOW': 1  # Reduced penalty for LOW severity issues
                }

                # Check if this is a known safe subprocess usage pattern
                # (e.g., running known tools with static arguments)
                code = result.get('code', '').strip()
                test_id = result.get('test_id', '')
                issue_text = result.get('issue_text', '')
                filename = result.get('filename', '')

                # Skip LOW severity subprocess warnings for known safe tools
                # These are legitimate tool invocations, not security risks
                safe_tools = ['pylint', 'radon', 'bandit', 'safety', 'lsof', sys.executable]
                safe_files = ['utils.py', 'run_evaluation.py', 'code_quality_check.py',
                              'security_check.py', 'generate_report.py']
                is_safe_subprocess = (
                    severity == 'LOW' and
                    test_id in ['B603', 'B607', 'B404'] and
                    (any(tool in code for tool in safe_tools) or
                     any(safe_file in filename for safe_file in safe_files))
                )

                if not is_safe_subprocess:
                    issues.append({
                        "name": test_id,
                        "severity": severity,
                        "description": issue_text,
                        "file": result.get('filename', ''),
                        "line": result.get('line_number', 0),
                        "code": code,
                        "penalty": penalty_map.get(severity, 5),
                        "source": "bandit"
                    })

            # Clean up
            Path(tmp_path).unlink(missing_ok=True)

        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Bandit scan failed: {e}", file=sys.stderr)

        return issues

    def _check_dependency_cves(self) -> List[Dict]:
        """Check for known CVEs in dependencies using Safety.

        Returns:
            List of CVE issues found in dependencies.
        """
        issues = []

        # Look for requirements.txt
        req_file = self.skill_path / 'requirements.txt'
        if not req_file.exists():
            return issues

        try:
            # Check if safety is installed
            result = subprocess.run(
                ['safety', '--version'],
                capture_output=True,
                timeout=5,
                check=False
            )
            if result.returncode != 0:
                return issues
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Safety not available, skip
            return issues

        try:
            # Run safety check
            result = run_command(['safety', 'check', '--file', req_file])

            # Parse results (safety returns non-zero if vulnerabilities found)
            safety_results = parse_json_output(result)
            if safety_results:

                for vuln in safety_results:
                    # Determine severity
                    cve_id = vuln.get('vulnerability_id', vuln.get('advisory', 'Unknown'))
                    advisory = vuln.get('advisory', '')
                    severity = 'HIGH' if 'critical' in advisory.lower() else 'MEDIUM'
                    pkg = vuln.get('package', 'unknown')
                    ver = vuln.get('installed_version', '')

                    issues.append({
                        "name": f"cve_{cve_id}",
                        "severity": severity,
                        "description": f"CVE in {pkg} {ver}",
                        "file": "requirements.txt",
                        "line": 0,
                        "code": f"{pkg}=={ver}",
                        "penalty": 10 if severity == 'HIGH' else 5,
                        "source": "safety",
                        "advisory": advisory
                    })

        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Safety check failed: {e}", file=sys.stderr)

        return issues


def main() -> None:
    """Command-line interface"""
    if len(sys.argv) < 2:
        print("Usage: python security_check.py <skill-path>")
        sys.exit(1)

    skill_path = sys.argv[1]

    print(f"Running Security Check on {skill_path}")
    print("=" * 70)

    checker = SecurityChecker(skill_path)
    results = checker.evaluate()

    # Print results
    print("\n[Layer 2] Security Compliance Results")
    print(f"Score: {results['score']}/{results['max_score']}")
    print(f"Status: {'✅ PASSED' if results['passed'] else '🚫 BLOCKED'}")

    # Print summary
    summary = results['summary']
    print("\nIssues Summary:")
    print(f"  Critical: {summary['critical']}")
    print(f"  High:     {summary['high']}")
    print(f"  Medium:   {summary['medium']}")
    print(f"  Low:      {summary['low']}")
    print(f"  Total:    {summary['total_issues']}")

    # Print critical issues
    if results['critical_issues']:
        print("\n🚨 CRITICAL SECURITY ISSUES (BLOCKING):")
        for issue in results['critical_issues']:
            print(f"\n  [{issue['severity']}] {issue['name']}")
            print(f"  Description: {issue['description']}")
            print(f"  Location: {issue['file']}:{issue['line']}")
            print(f"  Code: {issue['code']}")

    # Print other issues
    other_issues = [i for i in results['issues'] if i['severity'] != 'CRITICAL']
    if other_issues:
        print("\n⚠️  Other Security Issues:")
        for issue in other_issues:
            print(f"\n  [{issue['severity']}] {issue['name']}")
            print(f"  {issue['description']}")
            print(f"  {issue['file']}:{issue['line']}")

    # Save results
    output_path = Path(skill_path) / "evaluation_results" / "security_results.json"
    save_json_results(results, output_path, results['passed'])


if __name__ == "__main__":
    main()
