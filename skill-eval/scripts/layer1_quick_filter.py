#!/usr/bin/env python3
"""
Layer 1: Quick Filter
Fast screening to block obviously non-compliant skills (<30s)
Checks metadata, documentation completeness, and basic compliance
"""

import re
import sys
from pathlib import Path
from typing import Dict
import yaml

from utils import save_json_results


# pylint: disable=too-few-public-methods
class QuickFilter:
    """Layer 1 evaluation: Quick filter for basic compliance.

    Fast screening (<30s) to block obviously non-compliant skills.
    Checks metadata validation, documentation completeness, and basic compliance.

    Scoring breakdown (20 points total):
    - Metadata validation: 5 points
    - Documentation completeness: 10 points
    - Basic compliance: 5 points

    Passing threshold: 15/20 points
    """

    def __init__(self, skill_path: str):
        """Initialize the quick filter evaluator.

        Args:
            skill_path: Path to the skill directory to evaluate.
        """
        self.skill_path = Path(skill_path)
        self.skill_md_path = self.skill_path / "SKILL.md"
        self.results = {
            "layer": "1_quick_filter",
            "score": 0,
            "max_score": 20,
            "passed": False,
            "threshold": 15,
            "checks": {},
            "issues": [],
            "recommendations": []
        }

    def evaluate(self) -> Dict:
        """Run all Layer 1 checks.

        Returns:
            Dictionary containing score, passed status, check breakdown, and issues.
        """

        # Check 1: Metadata validation (5 points)
        metadata_score = self._check_metadata()

        # Check 2: Documentation completeness (10 points)
        doc_score = self._check_documentation()

        # Check 3: Basic compliance (5 points)
        compliance_score = self._check_basic_compliance()

        # Calculate total score
        self.results["score"] = metadata_score + doc_score + compliance_score
        self.results["passed"] = self.results["score"] >= self.results["threshold"]

        # Generate recommendations if not passed
        if not self.results["passed"]:
            self._generate_recommendations()

        return self.results

    def _check_metadata(self) -> int:
        """Check metadata validation (5 points).

        Validates:
        - SKILL.md file existence
        - YAML frontmatter presence and validity
        - Required fields (name, description)
        - Name format (kebab-case) and directory match

        Returns:
            Score from 0-5 based on metadata quality.
        """
        score = 5
        issues = []

        # Check SKILL.md exists
        if not self.skill_md_path.exists():
            score = 0
            issues.append("SKILL.md file not found")
            self.results["checks"]["metadata"] = {
                "score": 0,
                "max_score": 5,
                "issues": issues
            }
            self.results["issues"].extend(issues)
            return 0

        # Parse SKILL.md
        try:
            content = self.skill_md_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as e:
            score = 0
            issues.append(f"Cannot read SKILL.md: {e}")
            self.results["checks"]["metadata"] = {
                "score": 0,
                "max_score": 5,
                "issues": issues
            }
            self.results["issues"].extend(issues)
            return 0

        # Extract YAML frontmatter
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not frontmatter_match:
            score -= 2
            issues.append("YAML frontmatter missing or malformed")
        else:
            try:
                frontmatter = yaml.safe_load(frontmatter_match.group(1))

                # Check required fields
                if not frontmatter.get('name'):
                    score -= 1
                    issues.append("Missing 'name' field in frontmatter")
                else:
                    # Validate name format (kebab-case)
                    name = frontmatter['name']
                    if not re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', name):
                        score -= 0.5
                        msg = f"Name '{name}' should be kebab-case (lowercase with hyphens)"
                        issues.append(msg)

                    # Check name matches directory
                    if self.skill_path.name != name:
                        score -= 0.5
                        msg = f"Directory name '{self.skill_path.name}' doesn't match '{name}'"
                        issues.append(msg)

                if not frontmatter.get('description'):
                    score -= 2
                    issues.append("Missing 'description' field in frontmatter")

            except yaml.YAMLError as e:
                score -= 2
                issues.append(f"Invalid YAML frontmatter: {e}")

        self.results["checks"]["metadata"] = {
            "score": max(0, score),
            "max_score": 5,
            "issues": issues
        }
        self.results["issues"].extend(issues)

        return max(0, score)

    def _check_documentation(self) -> int:
        """Check documentation completeness (10 points).

        Validates:
        - Description length (>=50 chars): 2 points
        - Usage instructions: 3 points
        - Examples/use cases: 3 points
        - Trigger conditions: 2 points

        Returns:
            Score from 0-10 based on documentation quality.
        """
        score = 10
        issues = []

        if not self.skill_md_path.exists():
            self.results["checks"]["documentation"] = {
                "score": 0,
                "max_score": 10,
                "issues": ["SKILL.md not found"]
            }
            return 0

        content = self.skill_md_path.read_text(encoding='utf-8')
        body, frontmatter = self._parse_frontmatter(content)

        # Check 1: Description length (2 points)
        score, issues = self._check_description(frontmatter, score, issues)

        # Check 2: Body has usage instructions (3 points)
        score, issues = self._check_usage_instructions(body, score, issues)

        # Check 3: Contains examples or use cases (3 points)
        score, issues = self._check_examples(body, score, issues)

        # Check 4: Clear trigger conditions (2 points)
        score, issues = self._check_triggers(body, frontmatter, score, issues)

        self.results["checks"]["documentation"] = {
            "score": max(0, score),
            "max_score": 10,
            "issues": issues
        }
        self.results["issues"].extend(issues)

        return max(0, score)

    def _parse_frontmatter(self, content: str) -> tuple:
        """Parse frontmatter from SKILL.md content.

        Args:
            content: Raw file content.

        Returns:
            Tuple of (body_text, frontmatter_dict).
        """
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if frontmatter_match:
            body = content[frontmatter_match.end():]
            frontmatter_text = frontmatter_match.group(1)
            try:
                frontmatter = yaml.safe_load(frontmatter_text) or {}
            except yaml.YAMLError:
                frontmatter = {}
        else:
            body = content
            frontmatter = {}
        return body, frontmatter

    def _check_description(self, frontmatter: dict, score: int, issues: list) -> tuple:
        """Check description length.

        Args:
            frontmatter: Parsed frontmatter dict.
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        description = frontmatter.get('description', '')
        if len(description) < 50:
            score -= 2
            issues.append(f"Description too short ({len(description)} chars, need ≥50)")
        return score, issues

    def _check_usage_instructions(self, body: str, score: int, issues: list) -> tuple:
        """Check for usage instructions in body.

        Args:
            body: Body text from SKILL.md.
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        body_lower = body.lower()
        usage_keywords = [
            # 英文关键词
            'usage', 'how to use', 'getting started', 'quick start',
            'instructions', 'guide', 'workflow', 'step', 'run ', 'execute',
            # 中文关键词
            '用法', '使用方法', '快速开始', '使用说明', '操作步骤',
            '流程', '工作流', '如何使用', '运行', '执行', '示例',
        ]
        has_usage = any(kw in body_lower for kw in usage_keywords)
        if not has_usage:
            score -= 3
            issues.append("未找到清晰的使用说明 (No clear usage instructions found)")
        return score, issues

    def _check_examples(self, body: str, score: int, issues: list) -> tuple:
        """Check for examples or use cases in body.

        Args:
            body: Body text from SKILL.md.
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        body_lower = body.lower()
        example_keywords = [
            # 英文
            'example', 'use case', 'demonstration', 'sample', 'scenario',
            # 中文
            '示例', '用例', '案例', '样例', '场景', '例如', '比如',
        ]
        has_examples = any(kw in body_lower for kw in example_keywords)
        code_blocks = len(re.findall(r'```', body))
        has_code_examples = code_blocks >= 2

        if not has_examples and not has_code_examples:
            score -= 3
            issues.append("No examples or use cases found")
        elif not has_examples and has_code_examples:
            score -= 1
            issues.append("Code examples found but not clearly labeled")
        return score, issues

    def _check_triggers(self, body: str, frontmatter: dict,
                        score: int, issues: list) -> tuple:
        """Check for trigger conditions in body and description.

        Args:
            body: Body text from SKILL.md.
            frontmatter: Parsed frontmatter dict.
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        body_lower = body.lower()
        trigger_keywords = [
            # 英文
            'when', 'trigger', 'use this', 'invoke', 'activate', 'use when',
            # 中文
            '当用户', '触发', '适用于', '使用时', '使用场景', '触发条件',
            '什么时候', '何时使用', '触发关键词',
        ]
        has_triggers = any(kw in body_lower for kw in trigger_keywords)
        description = frontmatter.get('description', '')
        desc_has_triggers = any(kw in description.lower() for kw in trigger_keywords)

        if not has_triggers and not desc_has_triggers:
            score -= 2
            issues.append("触发条件未明确描述 (Trigger conditions not clearly described)")
        return score, issues

    def _check_basic_compliance(self) -> int:
        """Check basic compliance (5 points).

        Validates:
        - No suspicious filenames (executables, malware patterns): 2 points
        - Reasonable file sizes (<10MB each): 2 points
        - No malicious patterns (git hooks, __pycache__): 1 point

        Returns:
            Score from 0-5 based on compliance.
        """
        score = 5
        issues = []

        # Check 1: No suspicious filenames (2 points)
        score, issues = self._check_suspicious_files(score, issues)

        # Check 2: File sizes reasonable (2 points)
        score, issues = self._check_file_sizes(score, issues)

        # Check 3: No obviously malicious file patterns (1 point)
        score, issues = self._check_malicious_patterns(score, issues)

        self.results["checks"]["basic_compliance"] = {
            "score": max(0, score),
            "max_score": 5,
            "issues": issues
        }
        self.results["issues"].extend(issues)

        return max(0, score)

    def _check_suspicious_files(self, score: int, issues: list) -> tuple:
        """Check for suspicious filenames.

        Args:
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        suspicious_patterns = [
            r'\.exe$', r'\.dll$', r'\.so$', r'\.dylib$',
            r'malware', r'virus', r'hack', r'crack',
            r'\.pem$', r'\.key$', r'\.p12$',
        ]

        for file_path in self.skill_path.rglob('*'):
            if file_path.is_file():
                filename = file_path.name.lower()
                for pattern in suspicious_patterns:
                    if re.search(pattern, filename):
                        score -= 1
                        rel_path = file_path.relative_to(self.skill_path)
                        issues.append(f"Suspicious filename: {rel_path}")
                        break
        return score, issues

    def _check_file_sizes(self, score: int, issues: list) -> tuple:
        """Check for oversized files.

        Args:
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        max_file_size = 10 * 1024 * 1024  # 10MB
        large_files = []

        for file_path in self.skill_path.rglob('*'):
            if file_path.is_file():
                size = file_path.stat().st_size
                if size > max_file_size:
                    large_files.append((file_path.relative_to(self.skill_path), size))

        if large_files:
            score -= 2
            for file_rel, size in large_files:
                size_mb = size / (1024 * 1024)
                issues.append(f"Large file: {file_rel} ({size_mb:.1f}MB > 10MB limit)")
        return score, issues

    def _check_malicious_patterns(self, score: int, issues: list) -> tuple:
        """Check for malicious file patterns.

        Args:
            score: Current score.
            issues: Current issues list.

        Returns:
            Tuple of (updated_score, updated_issues).
        """
        malicious_indicators = [
            '.git/hooks/',
            '__pycache__/',
            '.pyc',
        ]

        for file_path in self.skill_path.rglob('*'):
            file_str = str(file_path.relative_to(self.skill_path))
            for indicator in malicious_indicators:
                if indicator in file_str:
                    score -= 0.5
                    issues.append(f"Should not commit: {file_str}")
                    break
        return score, issues

    def _generate_recommendations(self):
        """Generate improvement recommendations"""
        recommendations = []

        # Recommendations based on issues
        metadata_check = self.results["checks"].get("metadata", {})
        if metadata_check.get("score", 0) < 4:
            recommendations.append({
                "priority": "P0",
                "category": "Metadata",
                "suggestion": "Add complete YAML frontmatter with 'name' and 'description'",
                "example": "---\nname: my-skill\ndescription: Brief description\n---"
            })

        doc_check = self.results["checks"].get("documentation", {})
        if doc_check.get("score", 0) < 7:
            recommendations.append({
                "priority": "P0",
                "category": "Documentation",
                "suggestion": "Add comprehensive documentation with usage instructions",
                "sections": [
                    "What the skill does (Overview)",
                    "When to use it (Trigger conditions)",
                    "How to use it (Instructions)",
                    "Examples (Code samples or use cases)"
                ]
            })

        compliance_check = self.results["checks"].get("basic_compliance", {})
        if compliance_check.get("score", 0) < 4:
            recommendations.append({
                "priority": "P1",
                "category": "Compliance",
                "suggestion": "Remove large files, compiled bytecode, and sensitive files",
                "action": "Add .gitignore and clean up repository"
            })

        self.results["recommendations"] = recommendations


def main() -> None:
    """Command-line interface"""
    if len(sys.argv) < 2:
        print("Usage: python layer1_quick_filter.py <skill-path>")
        sys.exit(1)

    skill_path = sys.argv[1]

    print(f"Running Layer 1: Quick Filter on {skill_path}")
    print("=" * 70)

    evaluator = QuickFilter(skill_path)
    results = evaluator.evaluate()

    # Print results
    print("\n[Layer 1] Quick Filter Results")
    print(f"Score: {results['score']}/{results['max_score']}")
    print(f"Status: {'✅ PASSED' if results['passed'] else '❌ BLOCKED'}")
    print(f"Threshold: {results['threshold']}/{results['max_score']}")

    # Print check breakdown
    print("\nCheck Breakdown:")
    for check_name, check_data in results['checks'].items():
        print(f"  {check_name}: {check_data['score']}/{check_data['max_score']}")
        if check_data['issues']:
            for issue in check_data['issues']:
                print(f"    ⚠️  {issue}")

    # Print recommendations
    if results['recommendations']:
        print("\n📋 Recommendations:")
        for rec in results['recommendations']:
            print(f"  [{rec['priority']}] {rec['category']}: {rec['suggestion']}")

    # Save results to JSON
    output_path = Path(skill_path) / "evaluation_results" / "layer1_results.json"
    save_json_results(results, output_path, results['passed'])


if __name__ == "__main__":
    main()
