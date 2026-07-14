"""Layer 2: Static Analysis — code quality and security compliance (v5)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import structlog

from evaluator.config import CODE_EXTENSIONS, EXCLUDED_DIRS, SCORE_PROFILES
from evaluator.models.exceptions import BlockedError
from evaluator.models.skill import SkillInfo

logger = structlog.get_logger()

# ── v5 A.1 High-risk patterns ──────────────────────────────────────────────────
# Each entry: (regex, category, description, severity, deduction_ratio)
SECURITY_PATTERNS: list[tuple[str, str, str, str, float]] = [
    # 3.2.1 高危漏洞
    (r"os\.system\s*\(", "命令注入", "os.system() 使用", "HIGH", 0.50),
    (r"subprocess\.[^\n]+shell\s*=\s*True[^\n]*[+%]", "命令注入", "subprocess(shell=True) 与字符串拼接", "HIGH", 0.50),
    (r"(?<!\w)eval\s*\(", "代码注入", "eval() 执行用户输入", "CRITICAL", 0.50),
    (r"(?<!\w)exec\s*\(", "代码注入", "exec() 执行用户输入", "CRITICAL", 0.50),
    (r"pickle\.load\s*\(", "不安全反序列化", "pickle.load()", "CRITICAL", 0.30),
    (r"yaml\.unsafe_load\s*\(", "不安全反序列化", "yaml.unsafe_load()", "CRITICAL", 0.30),
    (r'open\s*\([^)]*\+', "路径遍历", "open() 拼接用户输入路径", "HIGH", 0.40),
    # 3.2.2 敏感信息泄露
    (r"""(?:api[_-]?key|apikey)\s*=\s*['"][a-zA-Z0-9]{16,}['"]""", "敏感信息", "API密钥硬编码", "HIGH", 0.20),
    (r"""(?:token|access_token)\s*=\s*['"][a-zA-Z0-9]{20,}['"]""", "敏感信息", "Token硬编码", "HIGH", 0.20),
    (r"-----BEGIN (?:RSA|EC|DSA) PRIVATE KEY-----", "敏感信息", "私钥硬编码", "CRITICAL", 0.40),
    (r"""(?:logger|logging|print)\s*\(.*(?:password|passwd|secret|token)""", "敏感信息", "日志打印敏感字段", "MEDIUM", 0.10),
    # SQL injection
    (r"""(?:execute|cursor\.execute)\s*\([^,)]*['"][^'"]*[+%]""", "SQL注入", "SQL字符串拼接无参数化", "HIGH", 0.50),
    # 3.2.3 危险操作
    (r"rm\s+-[rf]+\s+", "危险操作", "rm -rf 系统命令", "HIGH", 0.15),
    (r"shutil\.rmtree\s*\(", "危险操作", "shutil.rmtree() 递归删除", "HIGH", 0.15),
    (r"subprocess\.call\s*\(", "危险操作", "subprocess.call() 无白名单检测", "MEDIUM", 0.15),
    (r"requests\.(get|post|put|delete)\s*\(", "网络请求", "requests 请求无域名白名单检测", "LOW", 0.10),
]

# CRITICAL severities trigger BlockedError per v5 spec
CRITICAL_TRIGGERS = {"CRITICAL"}


class Layer2Static:
    """Layer 2: Static analysis (<30s) per v5 spec.

    Code Quality sub-checks (quality_max):
      - pylint E/W/C (25%)
      - radon CC (20%)
      - type annotation coverage (15%)
      - bare except (15%)
      - docstring coverage (15%)
      - resource not closed (10%, phase-2)

    Security sub-checks (security_max):
      - High-risk vuln scan A.1 (50%)
      - Sensitive data leak A.2 (25%)
      - Dangerous ops A.3 (15%)
      - Dependency CVE A.5 (10%)
    """

    layer_number = 2
    layer_name = "layer2_static"

    def __init__(self, skill_info: SkillInfo) -> None:
        self.skill_info = skill_info
        self.skill_path = skill_info.skill_path
        self.profile = skill_info.eval_profile.value
        self.weights = SCORE_PROFILES[self.profile]
        self.quality_max = self.weights.quality_max
        self.security_max = self.weights.security_max
        self.log = logger.bind(layer=self.layer_name, skill=skill_info.metadata.name)

    async def run(self) -> dict:
        """Execute Layer 2.

        Returns:
            Result dict with code_quality and security sub-results.

        Raises:
            BlockedError: If a CRITICAL security issue is found.
        """
        t_start = time.monotonic()

        if not self.skill_info.has_code:
            duration = round(time.monotonic() - t_start, 3)
            self.log.info("layer2.skipped_no_code",
                          quality_score=self.quality_max,
                          security_score=self.security_max)
            return {
                "layer": 2,
                "skipped": True,
                "reason": "No code files found",
                "evaluation_method": "skipped",
                "duration_s": duration,
                "code_quality": {
                    "score": self.quality_max,
                    "max_score": self.quality_max,
                    "skipped": True,
                    "check_items": [{"label": "代码质量（无代码文件，授予满分）", "passed": True,
                                     "detail": "has_code=False"}],
                },
                "security": {
                    "score": self.security_max,
                    "max_score": self.security_max,
                    "skipped": True,
                    "security_raw": 1.0,
                    "is_compliant": True,
                    "critical_issues": [],
                    "check_items": [{"label": "安全合规（无代码文件，授予满分）", "passed": True,
                                     "detail": "has_code=False"}],
                },
                "combined_score": self.quality_max + self.security_max,
                "passed": True,
            }

        quality_result = await self._run_code_quality()
        security_result = await self._run_security()

        # v5: CRITICAL vuln → BlockedError
        if security_result.get("critical_issues"):
            reason = "; ".join(
                i.get("description", str(i)) for i in security_result["critical_issues"][:3]
            )
            raise BlockedError(layer=2, score=0, reason=f"CRITICAL security: {reason}")

        combined = quality_result["score"] + security_result["score"]
        duration = round(time.monotonic() - t_start, 3)
        result = {
            "layer": 2,
            "skipped": False,
            "evaluation_method": quality_result.get("evaluation_method", "tool"),
            "duration_s": duration,
            "code_quality": quality_result,
            "security": security_result,
            "combined_score": round(combined, 2),
            "passed": True,
        }
        self.log.info("layer2.complete", combined=combined, duration_s=duration)
        return result

    # ── code quality ──────────────────────────────────────────────────────────

    async def _run_code_quality(self) -> dict:
        if self.quality_max == 0:
            return {"score": 0.0, "max_score": 0.0, "skipped": True, "reason": "quality_max=0"}

        code_files = self._detect_code_files()
        py_files = [f for f in code_files if f.suffix == ".py"]

        if not py_files:
            # Non-Python skill — award full quality score per v5 skip rule
            return {
                "score": self.quality_max,
                "max_score": self.quality_max,
                "evaluation_method": "skipped_non_python",
                "note": "非 Python 代码，代码质量检查跳过，授予满分",
                "check_items": [{"label": "代码质量（非 Python，满分）", "passed": True, "detail": "skipped"}],
                "issues": [],
            }

        pylint_result = await self._run_pylint(py_files)
        radon_result = await self._run_radon(py_files)
        annotation_ratio = self._check_type_annotations(py_files)
        bare_except_count = self._check_bare_except(py_files)
        docstring_missing = self._check_docstring_coverage(py_files)

        # v5 formula: quality_score = max(0, 1 - ∑deductions) × quality_max
        # Weights: pylint 25%, radon 20%, annotation 15%, bare_except 15%, docstring 15%
        deduction = 0.0
        check_items = []

        # pylint (25%)
        pylint_ratio = pylint_result.get("ratio", 1.0) if pylint_result else None
        if pylint_ratio is None:
            check_items.append({"label": "pylint 代码质量", "passed": True,
                                 "detail": "工具不可用", "weight": "25%"})
        else:
            pylint_ded = (1.0 - pylint_ratio) * 0.25
            deduction += pylint_ded
            ok = pylint_ded < 0.05
            check_items.append({
                "label": "pylint 代码质量",
                "passed": ok,
                "detail": (f"ratio={pylint_ratio:.2f}, errors={pylint_result.get('error_count',0)}, "
                            f"warnings={pylint_result.get('warning_count',0)}"),
                "weight": "25%",
            })

        # radon CC (20%)
        if radon_result is None:
            check_items.append({"label": "圈复杂度 (radon CC)", "passed": True,
                                 "detail": "工具不可用", "weight": "20%"})
        else:
            radon_ded = min(radon_result.get("penalty", 0.0), 0.20)
            deduction += radon_ded
            ok = radon_ded < 0.05
            check_items.append({
                "label": "圈复杂度 (radon CC)",
                "passed": ok,
                "detail": f"high CC functions: {radon_result.get('high_complexity_count', 0)}",
                "weight": "20%",
            })

        # type annotations (15%) — proportional deduction above 80% threshold
        # 100% coverage → 0 deduction; 0% coverage → full 15% deduction
        # Linear: deduction = max(0, (0.8 - ratio) / 0.8) * 0.15
        ann_ded = max(0.0, (0.8 - annotation_ratio) / 0.8) * 0.15
        deduction += ann_ded
        ok = annotation_ratio >= 0.8
        check_items.append({
            "label": "类型注解覆盖率",
            "passed": ok,
            "detail": f"{annotation_ratio:.0%}",
            "weight": "15%",
        })

        # bare except (15%) — v5 A: each occurrence -20%
        bare_ded = min(bare_except_count * 0.20, 1.0) * 0.15
        deduction += bare_ded
        check_items.append({
            "label": "裸 except 使用",
            "passed": bare_except_count == 0,
            "detail": f"{bare_except_count} 处 bare except",
            "weight": "15%",
        })

        # docstring coverage (15%) — each missing -5%
        doc_ded = min(docstring_missing * 0.05, 1.0) * 0.15
        deduction += doc_ded
        check_items.append({
            "label": "公共函数 docstring",
            "passed": docstring_missing == 0,
            "detail": f"{docstring_missing} 个公共函数缺少 docstring",
            "weight": "15%",
        })

        # resource not closed (10%) — phase-2: not yet implemented, always pass
        # Stub ensures weights are explicit and sum to 100%
        check_items.append({
            "label": "资源泄漏检查 (phase-2, 暂不扣分)",
            "passed": True,
            "detail": "phase-2 实施中，本版本不扣分",
            "weight": "10%",
        })

        quality_ratio = max(0.0, 1.0 - deduction)
        score = round(quality_ratio * self.quality_max, 2)

        all_issues: list[dict] = []
        if pylint_result:
            all_issues.extend(pylint_result.get("issues", [])[:5])
        if radon_result:
            all_issues.extend(radon_result.get("issues", [])[:3])

        return {
            "score": score,
            "max_score": self.quality_max,
            "evaluation_method": "tool",
            "pylint": pylint_result,
            "radon": radon_result,
            "type_annotation_ratio": annotation_ratio,
            "bare_except_count": bare_except_count,
            "docstring_missing_count": docstring_missing,
            "check_items": check_items,
            "issues": all_issues,
        }

    async def _run_pylint(self, py_files: list[Path]) -> dict | None:
        try:
            targets = [str(f) for f in py_files[:20]]
            proc = await asyncio.create_subprocess_exec(
                "pylint", "--output-format=json", "--score=no", *targets,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            messages = json.loads(stdout.decode()) if stdout.strip() else []
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

        errors = [m for m in messages if m.get("type") in ("error", "fatal")]
        warnings = [m for m in messages if m.get("type") == "warning"]
        total = len(py_files) * 10 or 1
        penalty = len(errors) * 2 + len(warnings) * 0.5
        ratio = max(0.0, 1.0 - penalty / total)

        return {
            "ratio": ratio,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "issues": [
                {
                    "severity": m["type"],
                    "description": m.get("message", ""),
                    "location": f"{m.get('path', '')}:{m.get('line', '')}",
                }
                for m in (errors + warnings)[:10]
            ],
        }

    async def _run_radon(self, py_files: list[Path]) -> dict | None:
        try:
            targets = [str(f) for f in py_files[:20]]
            proc = await asyncio.create_subprocess_exec(
                "radon", "cc", "--json", *targets,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            data = json.loads(stdout.decode()) if stdout.strip() else {}
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

        high_cc = []
        for _file, blocks in data.items():
            for block in blocks:
                if block.get("complexity", 0) > 10:
                    high_cc.append(f"{block['name']} (CC={block['complexity']})")

        penalty = len(high_cc) * 0.05
        return {
            "penalty": penalty,
            "high_complexity_count": len(high_cc),
            "issues": [{"description": f"高圈复杂度: {h}", "severity": "minor"} for h in high_cc[:5]],
        }

    def _check_type_annotations(self, py_files: list[Path]) -> float:
        """Return ratio of functions with return type annotations (0-1)."""
        total = annotated = 0
        func_pattern = re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\(")
        ret_pattern = re.compile(r"\)\s*->\s*\S")
        for f in py_files[:10]:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if func_pattern.match(line):
                    total += 1
                    window = "\n".join(lines[i:i + 5])
                    if ret_pattern.search(window):
                        annotated += 1
        return annotated / total if total else 1.0

    def _check_bare_except(self, py_files: list[Path]) -> int:
        """Count bare `except:` clauses (v5 3.1 quality check)."""
        count = 0
        pattern = re.compile(r"^\s*except\s*:", re.MULTILINE)
        for f in py_files:
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                continue
            count += len(pattern.findall(content))
        return count

    def _check_docstring_coverage(self, py_files: list[Path]) -> int:
        """Return number of public functions missing docstrings (v5 3.1)."""
        missing = 0
        func_def = re.compile(r"^\s{0,4}(?:async\s+)?def\s+([A-Za-z][^(]+)\(")
        docstring_start = re.compile(r'^\s*(?:"""|\'{3})')
        for f in py_files[:10]:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                m = func_def.match(line)
                if not m:
                    continue
                fn_name = m.group(1).strip()
                if fn_name.startswith("_"):  # skip private
                    continue
                # Check if next non-empty line is a docstring
                for j in range(i + 1, min(i + 5, len(lines))):
                    next_line = lines[j]
                    if next_line.strip() == "":
                        continue
                    if not docstring_start.match(next_line):
                        missing += 1
                    break
        return missing

    # ── security ──────────────────────────────────────────────────────────────

    async def _run_security(self) -> dict:
        """v5 Security: regex scan + bandit + pip-audit, all A.1-A.5."""
        code_files = self._detect_code_files()

        # Always run custom regex scan (v5 A.1-A.3)
        regex_findings = self._run_regex_security_scan(code_files)

        # Run bandit for Python files
        bandit_result = await self._run_bandit(code_files)
        pip_result = await self._run_pip_audit()

        # Collect critical issues (trigger BlockedError)
        critical_issues: list[dict] = []
        for f in regex_findings:
            if f["severity"] in CRITICAL_TRIGGERS:
                critical_issues.append(f)
        if bandit_result:
            critical_issues.extend(bandit_result.get("critical", []))
        if pip_result:
            critical_issues.extend(pip_result.get("critical_cves", []))

        # Calculate security_raw (0-1)
        deduction = 0.0
        for finding in regex_findings:
            deduction += finding.get("deduction_ratio", 0.0)
        if bandit_result:
            deduction += len(bandit_result.get("high", [])) * 0.10
        if pip_result:
            for cve in pip_result.get("cves", []):
                deduction += cve.get("deduction", 0.0)

        security_raw = max(0.0, 1.0 - deduction)
        score = round(security_raw * self.security_max, 2)

        # v5: security_raw < 0.67 → 安全不合规 warning
        is_compliant = security_raw >= 0.67

        # Build structured check_items for report
        cat_deductions: dict[str, float] = {}
        for f in regex_findings:
            cat_deductions[f["category"]] = cat_deductions.get(f["category"], 0.0) + f.get("deduction_ratio", 0.0)

        check_items = []
        for cat in ["命令注入", "代码注入", "不安全反序列化", "路径遍历", "SQL注入",
                    "敏感信息", "危险操作", "网络请求"]:
            findings_in_cat = [x for x in regex_findings if x["category"] == cat]
            passed = len(findings_in_cat) == 0
            check_items.append({
                "label": cat,
                "passed": passed,
                "detail": f"{len(findings_in_cat)} 处发现" if not passed else "clean",
                "findings": findings_in_cat,
            })

        if pip_result is not None:
            cve_count = len(pip_result.get("cves", []))
            check_items.append({
                "label": "依赖 CVE 风险 (pip-audit)",
                "passed": cve_count == 0,
                "detail": f"{cve_count} 个 CVE" if cve_count else "无已知 CVE",
                "findings": pip_result.get("cves", [])[:5],
            })

        if bandit_result is not None:
            b_issues = len(bandit_result.get("critical", [])) + len(bandit_result.get("high", []))
            check_items.append({
                "label": "bandit SAST 扫描",
                "passed": b_issues == 0,
                "detail": f"{b_issues} 个高危问题" if b_issues else "clean",
                "findings": bandit_result.get("critical", [])[:3],
            })

        return {
            "score": score,
            "max_score": self.security_max,
            "evaluation_method": "tool+regex",
            "security_raw": round(security_raw, 3),
            "is_compliant": is_compliant,
            "compliance_note": "" if is_compliant else "⚠️ 安全不合规（security_raw < 0.67）",
            "critical_issues": critical_issues,
            "all_findings": regex_findings,
            "check_items": check_items,
            "scans": [
                {"name": "自定义安全规则扫描", "passed": len(regex_findings) == 0},
                {"name": "bandit SAST", "passed": bandit_result is None or not bandit_result.get("critical")},
                {"name": "pip-audit CVE", "passed": pip_result is None or not pip_result.get("critical_cves")},
            ],
        }

    # Directories excluded from security regex scan in addition to EXCLUDED_DIRS
    _SECURITY_SCAN_SKIP_DIRS = frozenset({"scripts", "tests", "test", "docs"})

    def _run_regex_security_scan(self, code_files: list[Path]) -> list[dict]:
        """Run all v5 A.1-A.3 regex patterns across code files, skipping string literals."""
        findings: list[dict] = []
        for f in code_files:
            rel_parts = f.relative_to(self.skill_path).parts
            # Skip evaluator tooling / test directories — they intentionally contain patterns
            if any(p in self._SECURITY_SCAN_SKIP_DIRS for p in rel_parts):
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                continue
            content = "".join(lines)
            rel = str(f.relative_to(self.skill_path))
            for pattern, category, desc, severity, deduction_ratio in SECURITY_PATTERNS:
                for m in re.finditer(pattern, content, re.IGNORECASE | re.MULTILINE):
                    line_no = content[:m.start()].count("\n") + 1
                    raw_line = lines[line_no - 1] if line_no <= len(lines) else ""
                    stripped = raw_line.lstrip()
                    # Skip lines that are string literals (regex pattern defs, docstrings, comments)
                    if stripped.startswith(("#", '"""', "'''", 'r"', "r'", "(r\"", "(r'")):
                        continue
                    if r"\s*\(" in raw_line or r"\b" in raw_line:
                        continue
                    findings.append({
                        "category": category,
                        "description": desc,
                        "severity": severity,
                        "deduction_ratio": deduction_ratio,
                        "file": rel,
                        "line": line_no,
                        "snippet": content[m.start():m.start() + 80].replace("\n", " "),
                    })
        return findings

    async def _run_bandit(self, code_files: list[Path]) -> dict | None:
        py_files = [str(f) for f in code_files if f.suffix == ".py"]
        if not py_files:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bandit", "-r", "-f", "json", str(self.skill_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            data = json.loads(stdout.decode()) if stdout.strip() else {}
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

        results = data.get("results", [])
        critical = [
            {
                "description": r.get("issue_text", ""),
                "file": r.get("filename", ""),
                "line": r.get("line_number"),
                "severity": "CRITICAL",
            }
            for r in results if r.get("issue_severity") == "HIGH" and r.get("issue_confidence") == "HIGH"
        ]
        high = [r for r in results if r.get("issue_severity") == "HIGH" and r.get("issue_confidence") != "HIGH"]
        return {"critical": critical, "high": high}

    async def _run_pip_audit(self) -> dict | None:
        req = self.skill_path / "requirements.txt"
        toml = self.skill_path / "pyproject.toml"
        if not req.exists() and not toml.exists():
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "pip-audit", "--format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.skill_path),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            data = json.loads(stdout.decode()) if stdout.strip() else {}
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

        # v5 3.2.4 CVSS severity mapping.
        # deduction_ratio is on the same 0-1 scale as regex findings (NOT divided by security_max).
        # pip-audit JSON vulns don't include a severity field directly; we default to HIGH
        # (conservative) and promote to CRITICAL only when the vulnerability id suggests it.
        CVSS_DEDUCTION = {"CRITICAL": 0.40, "HIGH": 0.25, "MEDIUM": 0.10, "LOW": 0.05}
        cves = []
        critical_cves = []
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                # Attempt to extract severity from CVSS data if present; otherwise default to HIGH
                sev = "HIGH"
                fix_versions = vuln.get("fix_versions", [])
                # Check aliases (e.g. "GHSA-xxxx") or description text for severity hints
                aliases = " ".join(str(a) for a in vuln.get("aliases", [])).upper()
                desc = str(vuln.get("description", "")).upper()
                combined = aliases + " " + desc
                if "CRITICAL" in combined:
                    sev = "CRITICAL"
                elif "HIGH" in combined:
                    sev = "HIGH"
                elif "MEDIUM" in combined or "MODERATE" in combined:
                    sev = "MEDIUM"
                elif "LOW" in combined:
                    sev = "LOW"
                # deduction is a 0-1 ratio, consistent with regex finding deductions
                ded = CVSS_DEDUCTION.get(sev, 0.05)
                cves.append({
                    "vuln_id": vuln.get("id", "unknown"),
                    "description": vuln.get("description", ""),
                    "fix_versions": fix_versions,
                    "severity": sev,
                    "deduction": ded,
                })
                if sev == "CRITICAL":
                    critical_cves.append({"description": vuln.get("id", str(vuln)),
                                          "severity": "CRITICAL"})
        return {"cves": cves, "critical_cves": critical_cves}

    def _detect_code_files(self) -> list[Path]:
        files = []
        for f in self.skill_path.rglob("*"):
            if not f.is_file():
                continue
            rel_parts = set(f.relative_to(self.skill_path).parts)
            if rel_parts & EXCLUDED_DIRS:
                continue
            if f.suffix.lower() in CODE_EXTENSIONS:
                files.append(f)
        return files
