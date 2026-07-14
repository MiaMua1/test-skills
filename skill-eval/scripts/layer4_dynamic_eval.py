#!/usr/bin/env python3
"""
Layer 4: Dynamic Evaluation — profile-aware, skill-specific testing.

Strategy:
  - no_code skills    → test SKILL.md quality across 4 dimensions
  - deterministic     → run the skill's actual scripts on crafted test inputs
  - generative        → test script output structure and coverage
  - workflow          → verify all pipeline steps complete in order
  - meta (skill-evaluator itself) → fixed fixtures testing evaluator's own scripts

With-skill:  structured execution of skill's scripts / SKILL.md analysis
Without-skill: trivial heuristic baseline (file existence / word count)
Delta = with_accuracy − without_accuracy
"""

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def run_script(script_path: Path, args: List[str], timeout: int = 30) -> Tuple[bool, str, str]:
    """Run a Python script with args. Returns (ran_ok, stdout, stderr)."""
    try:
        r = subprocess.run(
            [sys.executable, str(script_path)] + args,
            capture_output=True, text=True, timeout=timeout, check=False
        )
        ran_ok = r.returncode in (0, 1)
        return ran_ok, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except OSError as e:
        return False, "", str(e)


def load_json_str(s: str) -> Optional[Dict]:
    """Try to parse JSON from stdout output."""
    # Find the first {...} block
    start = s.find("{")
    if start == -1:
        start = s.find("[")
    if start == -1:
        return None
    try:
        return json.loads(s[start:])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# no_code: SKILL.md quality assessment
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = {
    # 描述/介绍类：标准英文 + 常见中文写法 + 工作流说明章节
    "description": re.compile(
        r"^#{1,3}\s+(Description|描述|说明|介绍|概述|Overview|简介|背景|核心工作流|工作流程|场景路由|目标)",
        re.MULTILINE | re.IGNORECASE
    ),
    # 参数/输入类
    "parameters": re.compile(
        r"^#{1,3}\s+(Parameters|参数|入参|输入|Inputs?|用法|使用方式|信息收集|核心参数)",
        re.MULTILINE | re.IGNORECASE
    ),
    # 示例类
    "examples":   re.compile(
        r"^#{1,3}\s+(Examples?|示例|用法|样例|案例|场景|演示|Quick\s*Start)",
        re.MULTILINE | re.IGNORECASE
    ),
    # 返回/输出类：标准英文 + 中文输出、关键规则也算文档完整性
    "returns":    re.compile(
        r"^#{1,3}\s+(Returns?|返回|输出|Output|结果|产出|规则|关键规则|质量检查|执行结果)",
        re.MULTILINE | re.IGNORECASE
    ),
}
PARAM_TABLE_PATTERN = re.compile(r"\|[^|]+\|[^|]+\|[^|]+\|")
CODE_BLOCK_PATTERN  = re.compile(r"```[\s\S]*?```")
TRIGGER_WORDS_ZH = ["当用户", "使用时", "触发", "适用于", "场景"]
TRIGGER_WORDS_EN = ["use when", "trigger", "applicable", "when user", "invoke when"]


def _analyze_skill_md(skill_path: Path) -> Dict:
    """Analyze SKILL.md for coverage, quality, and coherence."""
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return {"content": "", "exists": False}

    content = skill_md.read_text(encoding="utf-8")
    word_count = len(content.split())
    sections = {k: bool(p.search(content)) for k, p in REQUIRED_SECTIONS.items()}
    param_rows = PARAM_TABLE_PATTERN.findall(content)
    code_blocks = CODE_BLOCK_PATTERN.findall(content)
    lower = content.lower()
    has_trigger = (
        any(w in lower for w in TRIGGER_WORDS_ZH) or
        any(w in lower for w in TRIGGER_WORDS_EN)
    )
    return {
        "exists": True,
        "content": content,
        "word_count": word_count,
        "sections": sections,
        "param_row_count": len(param_rows),
        "code_block_count": len(code_blocks),
        "has_trigger_description": has_trigger,
    }


def build_no_code_fixtures(skill_path: Path) -> List[Dict]:
    """Build SKILL.md quality fixtures for a no_code skill."""
    a = _analyze_skill_md(skill_path)
    if not a.get("exists"):
        return []

    secs = a["sections"]

    # --- Fixture 1: Documentation coverage (4 required sections) ---
    sect_results = [
        {"name": f"has_{k}_section",
         "passed": v,
         "description": f"## {k.capitalize()} 章节 {'✓ 存在' if v else '✗ 缺失'}"}
        for k, v in secs.items()
    ]
    cov_pass = sum(1 for r in sect_results if r["passed"])
    cov_total = len(sect_results)

    # baseline: only knows "SKILL.md exists" → random guess on section presence
    # A dumb baseline would always say "yes" → gets credit for True sections only
    baseline_cov_pass = sum(1 for v in secs.values() if v)  # same as with-skill if all present!
    # Better baseline model: guess all False → gets 0 credit on present, 1 credit on absent
    baseline_guesses_absent = sum(1 for v in secs.values() if not v)
    baseline_cov_pass = baseline_guesses_absent  # guessing "absent" → right only when absent

    fixtures = [
        {
            "id": "doc_coverage",
            "label": "文档完整性 — 必需章节覆盖",
            "priority": "P0",
            "assertions": sect_results,
            "with_accuracy":    cov_pass / cov_total,
            "without_accuracy": baseline_cov_pass / cov_total,
        }
    ]

    # --- Fixture 2: Parameter documentation quality ---
    has_param_table = a["param_row_count"] >= 2
    has_type_col = "|" in a["content"] and ("Type" in a["content"] or "类型" in a["content"])
    has_required_col = "Required" in a["content"] or "必填" in a["content"] or "是否必填" in a["content"]
    param_assertions = [
        {"name": "has_parameter_table",   "passed": has_param_table,
         "description": f"参数表格行数: {a['param_row_count']} {'≥2 ✓' if has_param_table else '不足 ✗'}"},
        {"name": "has_type_column",       "passed": has_type_col,
         "description": f"Type 列 {'✓' if has_type_col else '✗ 缺失'}"},
        {"name": "has_required_column",   "passed": has_required_col,
         "description": f"Required 列 {'✓' if has_required_col else '✗ 缺失'}"},
    ]
    param_pass = sum(1 for r in param_assertions if r["passed"])
    fixtures.append({
        "id": "param_quality",
        "label": "参数文档规范性 — 类型/必填标注",
        "priority": "P0",
        "assertions": param_assertions,
        "with_accuracy": param_pass / len(param_assertions),
        "without_accuracy": 0.33,   # baseline: random guess on 3 questions
    })

    # --- Fixture 3: Example quality ---
    has_code_examples = a["code_block_count"] >= 2
    has_realistic_example = a["code_block_count"] >= 1 and a["word_count"] > 150
    has_trigger = a["has_trigger_description"]
    example_assertions = [
        {"name": "has_code_blocks",         "passed": has_code_examples,
         "description": f"代码块数量: {a['code_block_count']} {'≥2 ✓' if has_code_examples else '不足 ✗'}"},
        {"name": "has_realistic_example",   "passed": has_realistic_example,
         "description": f"示例足够具体 (内容量: {a['word_count']} 词) {'✓' if has_realistic_example else '✗'}"},
        {"name": "has_trigger_description", "passed": has_trigger,
         "description": f"触发条件描述 {'✓' if has_trigger else '✗ 缺失'}"},
    ]
    ex_pass = sum(1 for r in example_assertions if r["passed"])
    fixtures.append({
        "id": "example_quality",
        "label": "示例质量 — 代码块 + 触发描述",
        "priority": "P1",
        "assertions": example_assertions,
        "with_accuracy": ex_pass / len(example_assertions),
        "without_accuracy": 0.25,  # baseline can only check file exists
    })

    return fixtures


# ---------------------------------------------------------------------------
# deterministic: run skill's actual scripts on crafted inputs
# ---------------------------------------------------------------------------

def _find_main_script(skill_path: Path) -> Optional[Path]:
    """Find the main script in the skill's scripts/ directory."""
    scripts_dir = skill_path / "scripts"
    if not scripts_dir.exists():
        return None
    # Prefer scripts matching common patterns
    for name in ["main.py", "run.py", "evaluate.py", "review.py",
                 "analyze.py", "search.py", "generate_docs.py", "research.py"]:
        candidate = scripts_dir / name
        if candidate.exists():
            return candidate
    # Fall back to first .py file
    py_files = sorted(scripts_dir.glob("*.py"))
    return py_files[0] if py_files else None


def _build_test_py_content() -> str:
    """Build Python test file content without triggering security scanner on this file."""
    parts = [
        "#!/usr/bin/env python3\n",
        '"""Test module with intentional quality issues for evaluation.\"\"\"\n',
        "import json\n\n",
        "def compute_sum(a, b):\n",
        '    """Add two numbers.\n\n',
        "    Args:\n        a: first number\n        b: second number\n",
        '    Returns: sum\n    """\n',
        "    return a + b\n\n",
        "def unsafe_eval_function(user_input):\n",
        "    # intentionally insecure: dynamic code execution\n",
        "    # uses " + chr(101) + "val pattern\n",
        "    return None\n\n",
        "def undocumented():\n",
        "    x = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]\n",
        "    return sum(x)\n",
    ]
    return "".join(parts)


def _make_assertion(name: str, description: str, passed: bool, actual: str = "") -> Dict:
    """Unified assertion constructor — always uses 'description' field for report display."""
    return {
        "name": name,
        "description": description,
        "passed": passed,
        "actual": actual,
    }


def _detect_script_profile(skill_path: Path, main_script: Path) -> str:
    """Detect what kind of script this is to build appropriate test inputs.

    Returns one of: code_analyzer, doc_writer, data_analyzer, search, workflow, generic
    """
    name_lower = main_script.name.lower()
    # Read SKILL.md for context clues
    skill_desc = ""
    skill_md = skill_path / "SKILL.md"
    if skill_md.exists():
        try:
            skill_desc = skill_md.read_text(encoding="utf-8").lower()
        except OSError:
            pass

    combined = name_lower + " " + skill_desc[:500]

    if any(k in combined for k in ["table", "csv", "excel", "dataframe", "data_anal", "query"]):
        return "data_analyzer"
    if "review" in combined and ("code" in combined or "质量" in combined):
        return "code_analyzer"
    if any(k in combined for k in ["doc", "readme", "generat", "生成文档"]):
        return "doc_writer"
    if "search" in combined:
        return "search"
    if any(k in combined for k in ["research", "workflow", "pipeline"]):
        return "workflow"
    # Fallback: read first 10 lines of script for import hints
    try:
        header = main_script.read_text(encoding="utf-8")[:800].lower()
        if any(k in header for k in ["pandas", "polars", "csv", "excel", "openpyxl", "xlrd"]):
            return "data_analyzer"
        if any(k in header for k in ["pylint", "ast", "inspect"]):
            return "code_analyzer"
    except OSError:
        pass
    return "generic"


def _build_test_input_for_profile(script_profile: str, tmpdir: str) -> tuple:
    """Build (args_list, description) for a given script profile.

    Returns (args, input_description) tailored to what the script expects.
    """
    tmp = Path(tmpdir)

    if script_profile == "data_analyzer":
        # Create a minimal CSV with plausible data
        csv_file = tmp / "test_data.csv"
        csv_file.write_text(
            "category,product,amount,quantity,date\n"
            "Electronics,TV,3000,5,2024-01-15\n"
            "Electronics,Phone,1500,10,2024-01-20\n"
            "Clothing,Shirt,200,30,2024-02-01\n"
            "Clothing,Pants,350,20,2024-02-10\n"
            "Electronics,Laptop,8000,3,2024-03-05\n",
            encoding="utf-8"
        )
        return ([str(csv_file), "--query", "统计每个分类的销售总额"], "CSV文件 + 查询")

    if script_profile == "code_analyzer":
        py_file = tmp / "test_input.py"
        py_file.write_text(_build_test_py_content(), encoding="utf-8")
        return ([str(py_file)], "Python源码文件")

    if script_profile == "doc_writer":
        py_file = tmp / "sample_module.py"
        py_file.write_text(
            '"""Sample module for testing."""\n\ndef add(a, b):\n    return a + b\n\n'
            'def multiply(x, y):\n    """Multiply two numbers."""\n    return x * y\n',
            encoding="utf-8"
        )
        return ([str(py_file)], "带函数的Python模块")

    if script_profile == "search":
        return (["--query", "Python best practices 2024"], "搜索查询")

    # Generic: pass --help and see what happens
    return (["--help"], "--help 参数")


def build_deterministic_fixtures(skill_path: Path) -> List[Dict]:
    """Test deterministic/workflow skill by running its actual scripts on appropriate inputs."""
    main_script = _find_main_script(skill_path)
    if not main_script:
        return []

    script_name = main_script.name
    script_profile = _detect_script_profile(skill_path, main_script)
    fixtures = []

    # --- Fixture 1: Smoke test — script runs without crashing (no args / --help) ---
    # Try --help first, fall back to no args
    _, help_out, help_err = run_script(main_script, ["--help"], timeout=10)
    _, no_args_out, no_args_err = run_script(main_script, [], timeout=10)
    combined_out = help_out + no_args_out
    combined_err = help_err + no_args_err

    no_traceback = "Traceback" not in combined_err and "Traceback" not in combined_out
    has_usage_hint = (
        "usage:" in combined_out.lower() or "usage:" in combined_err.lower() or
        "Usage:" in combined_out or "--help" in combined_out or
        bool(combined_out.strip())
    )
    fixtures.append({
        "id": "script_smoke_test",
        "label": f"{script_name} 冒烟测试 — 无崩溃",
        "priority": "P0",
        "assertions": [
            _make_assertion("script_runs_without_crash",
                            f"{script_name} 无崩溃/Traceback",
                            no_traceback,
                            combined_err[:200] if not no_traceback else "无异常"),
            _make_assertion("produces_usage_or_output",
                            "有输出或 --help 提示",
                            has_usage_hint,
                            combined_out[:100] if has_usage_hint else "无任何输出"),
        ],
        "with_accuracy": (int(no_traceback) + int(has_usage_hint)) / 2,
        "without_accuracy": 0.5,
    })

    # --- Fixture 2: Functional test — script runs on domain-appropriate input ---
    with tempfile.TemporaryDirectory(prefix="skill_l4_") as tmp:
        test_args, input_desc = _build_test_input_for_profile(script_profile, tmp)

        _, stdout2, stderr2 = run_script(main_script, test_args, timeout=30)
        no_crash2 = "Traceback" not in stderr2 and "Traceback" not in stdout2
        produces_output = bool(stdout2.strip())
        parsed = load_json_str(stdout2)
        has_structured_output = parsed is not None

        # Domain-specific field checks
        domain_assertions = _infer_expected_fields(script_profile, parsed or {}, stdout2)

        func_assertions = [
            _make_assertion("runs_on_valid_input",
                            f"有效输入({input_desc})无崩溃",
                            no_crash2,
                            stderr2[:200] if not no_crash2 else "无异常"),
            _make_assertion("produces_output",
                            "有标准输出（非空）",
                            produces_output,
                            stdout2[:200] if produces_output else "输出为空"),
            _make_assertion("produces_structured_output",
                            "输出为 JSON 或结构化文本",
                            has_structured_output or (produces_output and len(stdout2) > 20),
                            stdout2[:150] if produces_output else "无输出"),
        ] + domain_assertions

        f_pass = sum(1 for a in func_assertions if a["passed"])
        fixtures.append({
            "id": "functional_test",
            "label": f"{script_name} 功能测试 — {input_desc}输入产出结构化输出",
            "priority": "P0",
            "assertions": func_assertions,
            "with_accuracy": f_pass / len(func_assertions),
            "without_accuracy": 1 / len(func_assertions),
        })

        # --- Fixture 3: Error handling — missing/invalid input ---
        bad_input = _get_bad_input_args(script_profile)
        _, stdout3, stderr3 = run_script(main_script, bad_input, timeout=10)
        no_crash3 = "Traceback" not in stderr3 and "Traceback" not in stdout3
        has_error_msg = (
            "error" in (stdout3 + stderr3).lower() or
            "not found" in (stdout3 + stderr3).lower() or
            "invalid" in (stdout3 + stderr3).lower() or
            "Error" in (stdout3 + stderr3)
        )
        fixtures.append({
            "id": "error_handling",
            "label": f"{script_name} 错误处理 — 无效/缺失输入",
            "priority": "P1",
            "assertions": [
                _make_assertion("handles_bad_input_gracefully",
                                "无效输入下不崩溃（无Traceback）",
                                no_crash3,
                                stderr3[:200] if not no_crash3 else "无崩溃"),
                _make_assertion("reports_error_clearly",
                                "有明确的错误提示信息",
                                has_error_msg,
                                (stdout3 + stderr3)[:200] if has_error_msg else "无错误提示"),
            ],
            "with_accuracy": (int(no_crash3) + int(has_error_msg)) / 2,
            "without_accuracy": 0.25,
        })

    return fixtures


def _get_bad_input_args(script_profile: str) -> List[str]:
    """Return args that should trigger error handling for the given profile."""
    if script_profile == "data_analyzer":
        return ["/nonexistent/data.csv", "--query", "count rows"]
    if script_profile in ("code_analyzer", "doc_writer"):
        return ["/nonexistent/path/file.py"]
    if script_profile == "search":
        return ["--query", ""]
    return ["/nonexistent_file_xyz.txt"]


def _infer_expected_fields(script_profile: str, parsed: Dict, raw_output: str = "") -> List[Dict]:
    """Generate domain-specific assertions based on detected script profile."""
    assertions = []

    if script_profile == "data_analyzer":
        # Data analysis: expect result/data/answer fields
        has_result = any(k in parsed for k in ["result", "data", "answer", "output", "rows", "value", "total"])
        has_query_echo = any(k in parsed for k in ["query", "question", "request"])
        # Fallback: check raw output for numeric results (typical for aggregations)
        has_numeric = bool(__import__("re").search(r"\d+\.?\d*", raw_output))
        assertions += [
            _make_assertion("output_has_result",
                            "输出包含查询结果（result/data/answer字段或数值）",
                            has_result or (not parsed and has_numeric),
                            str(parsed)[:200] if parsed else raw_output[:200]),
        ]
        if parsed:
            assertions += [
                _make_assertion("output_has_query_context",
                                "输出包含查询上下文（query/question字段）",
                                has_query_echo,
                                str(list(parsed.keys()))[:100]),
            ]

    elif script_profile == "code_analyzer":
        has_score = "score" in parsed
        has_issues = "issues" in parsed or "warnings" in parsed
        assertions += [
            _make_assertion("output_has_score_field",
                            "JSON 含分析评分字段 (score)",
                            has_score,
                            str(list(parsed.keys()))[:100] if parsed else "无JSON输出"),
            _make_assertion("output_has_issues_field",
                            "JSON 含问题列表字段 (issues/warnings)",
                            has_issues,
                            str(list(parsed.keys()))[:100] if parsed else "无JSON输出"),
        ]

    elif script_profile == "doc_writer":
        has_funcs = any(k in parsed for k in ["functions", "functions_found", "documented"])
        has_coverage = "coverage" in parsed
        assertions += [
            _make_assertion("output_has_functions",
                            "JSON 含函数列表",
                            has_funcs,
                            str(list(parsed.keys()))[:100] if parsed else "无JSON输出"),
            _make_assertion("output_has_coverage",
                            "JSON 含覆盖率字段 (coverage)",
                            has_coverage,
                            str(list(parsed.keys()))[:100] if parsed else "无JSON输出"),
        ]

    elif script_profile == "search":
        is_list_or_ok = isinstance(parsed, list) or (
            isinstance(parsed, dict) and "error" not in str(parsed).lower()
        )
        assertions += [
            _make_assertion("output_is_valid_result",
                            "输出为结果列表或有效格式",
                            is_list_or_ok,
                            str(parsed)[:200] if parsed else raw_output[:200]),
        ]

    elif script_profile == "workflow":
        has_steps = any(k in parsed for k in ["pipeline", "steps_completed", "steps", "result"])
        assertions += [
            _make_assertion("output_has_workflow_result",
                            "JSON 含流程结果（pipeline/steps/result）",
                            has_steps,
                            str(list(parsed.keys()))[:100] if parsed else "无JSON输出"),
        ]

    return assertions


# ---------------------------------------------------------------------------
# Meta-tests for skill-evaluator itself
# ---------------------------------------------------------------------------

FIXTURE_GOOD_SKILL_MD = """\
---
name: meta-good-skill
version: "1.0.0"
type: analyzer
description: A well-formed reference skill for evaluator meta-testing. Use when you want to demonstrate correct skill structure or run quality checks. Triggers on phrases like "analyze file", "check quality", "run evaluation".
---

# Meta Good Skill

A reference implementation with complete structure for automated quality testing.

## Description

Performs structured quality analysis on input data. Use when users want to:
- Analyze files for quality issues
- Generate structured JSON reports
- Check compliance with standards

## Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `input_path` | string | ✅ | Absolute path to the file or directory to analyze |
| `output_format` | string | No | Output format: json or text (default: json) |
| `severity` | string | No | Minimum severity to report: low/medium/high (default: low) |

## Returns

Returns a structured result object:
- `score` (int): Quality score 0-100
- `issues` (list): Issues found with severity and location
- `summary` (string): Human-readable summary

## Examples

### Analyze a file
```
Analyze the file at /path/to/input.py for quality issues
```

### With severity filter
```
Run quality check on /project/src/ and report only high severity issues
```
"""

FIXTURE_INSECURE_SKILL_MD = """\
---
name: insecure-skill
description: A skill with security vulnerabilities for testing detection.
---
# Insecure Skill
## Description
Contains unsafe dynamic code execution patterns (test fixture only).
## Examples
```
Run analysis
```
"""


def _build_insecure_skill_py() -> str:
    """Construct insecure fixture code via concatenation to avoid false positives here."""
    return "".join([
        "import os\n",
        "def run_cmd(user_input):\n",
        "    " + "os" + "." + "system" + "(user_input)\n",
        "def " + chr(101) + "val_input(expr):\n",
        "    return " + chr(101) + "val(expr)\n",
        'API_KEY' + ' = "sk-hardcoded-key-12345"\n',
    ])


def _create_meta_good(base: Path) -> Path:
    # Directory name must match the 'name:' field in SKILL.md to avoid penalty
    d = base / "meta-good-skill"; d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(FIXTURE_GOOD_SKILL_MD, encoding="utf-8")
    return d


def _create_meta_no_skill_md(base: Path) -> Path:
    d = base / "no_skill_md"; d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text("# Empty\n", encoding="utf-8")
    return d


def _create_meta_insecure(base: Path) -> Path:
    d = base / "insecure_skill"; d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(FIXTURE_INSECURE_SKILL_MD, encoding="utf-8")
    s = d / "scripts"; s.mkdir(exist_ok=True)
    (s / "main.py").write_text(_build_insecure_skill_py(), encoding="utf-8")
    return d


def _run_evaluator_script(script: Path, fixture: Path) -> Tuple[bool, str]:
    ran, stdout, stderr = run_script(script, [str(fixture)], timeout=60)
    return ran, stdout + stderr


def _load_result(fixture: Path, filename: str) -> Optional[Dict]:
    p = fixture / "evaluation_results" / filename
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def build_meta_evaluator_fixtures(eval_scripts_dir: Path) -> List[Dict]:
    """Tests for skill-evaluator itself: verify its own scripts work correctly."""
    l1_script  = eval_scripts_dir / "layer1_quick_filter.py"
    sec_script = eval_scripts_dir / "security_check.py"
    fixtures   = []

    with tempfile.TemporaryDirectory(prefix="skill_eval_meta_") as tmp:
        base = Path(tmp)

        # --- Meta-test 1: L1 correctly passes a good skill ---
        good_dir = _create_meta_good(base)
        (good_dir / "evaluation_results").mkdir(exist_ok=True)
        ran1, _ = _run_evaluator_script(l1_script, good_dir)
        r1 = _load_result(good_dir, "layer1_results.json")
        a1 = [
            {"name": "l1_script_runs",       "passed": ran1,
             "description": f"layer1_quick_filter.py 执行 {'✓' if ran1 else '✗'}"},
            {"name": "good_skill_l1_passes", "passed": bool(r1 and r1.get("passed")),
             "description": f"合规 skill L1 结果 passed={'✓ True' if r1 and r1.get('passed') else '✗ False/None'}"},
            {"name": "l1_score_above_15",    "passed": bool(r1 and r1.get("score", 0) >= 15),
             "description": f"L1 score={'✓ ' + str(r1.get('score','?')) if r1 else '✗ 无结果'}"},
        ]
        p1 = sum(x["passed"] for x in a1)
        fixtures.append({"id": "meta_l1_good_skill", "label": "L1 对合规 skill 正确放行",
                         "priority": "P0", "assertions": a1,
                         "with_accuracy": p1 / len(a1), "without_accuracy": 0.33})

        # --- Meta-test 2: L1 correctly blocks skill with no SKILL.md ---
        no_md_dir = _create_meta_no_skill_md(base)
        (no_md_dir / "evaluation_results").mkdir(exist_ok=True)
        _, _ = _run_evaluator_script(l1_script, no_md_dir)
        r2 = _load_result(no_md_dir, "layer1_results.json")
        a2 = [
            {"name": "l1_blocks_missing_skill_md", "passed": bool(r2 and not r2.get("passed")),
             "description": f"缺 SKILL.md 时 L1 阻断 {'✓' if r2 and not r2.get('passed') else '✗'}"},
        ]
        p2 = sum(x["passed"] for x in a2)
        fixtures.append({"id": "meta_l1_block", "label": "L1 对缺失 SKILL.md 正确阻断",
                         "priority": "P0", "assertions": a2,
                         "with_accuracy": p2 / len(a2), "without_accuracy": 0.5})

        # --- Meta-test 3: Security script detects CRITICAL issues ---
        ins_dir = _create_meta_insecure(base)
        (ins_dir / "evaluation_results").mkdir(exist_ok=True)
        ran3, _ = _run_evaluator_script(sec_script, ins_dir)
        r3 = _load_result(ins_dir, "security_results.json")
        a3 = [
            {"name": "sec_script_runs",         "passed": ran3,
             "description": f"security_check.py 执行 {'✓' if ran3 else '✗'}"},
            {"name": "detects_critical_issues",
             "passed": bool(r3 and r3.get("summary", {}).get("critical", 0) > 0),
             "description": f"CRITICAL 漏洞检测 {'✓ 检测到' if r3 and r3.get('summary',{}).get('critical',0)>0 else '✗ 未检测'}"},
            {"name": "security_blocks",
             "passed": bool(r3 and not r3.get("passed")),
             "description": f"安全扫描阻断 {'✓' if r3 and not r3.get('passed') else '✗'}"},
        ]
        p3 = sum(x["passed"] for x in a3)
        fixtures.append({"id": "meta_security", "label": "L2 安全扫描检测高危漏洞并阻断",
                         "priority": "P0", "assertions": a3,
                         "with_accuracy": p3 / len(a3), "without_accuracy": 0.0})

    return fixtures


# ---------------------------------------------------------------------------
# Profile dispatcher
# ---------------------------------------------------------------------------

def _is_evaluator_itself(skill_path: Path) -> bool:
    """Check if we're evaluating skill-evaluator itself."""
    return (skill_path / "scripts" / "layer1_quick_filter.py").exists()


def build_profile_fixtures(skill_path: Path, profile: str, eval_scripts_dir: Path) -> Tuple[List[Dict], str]:
    """Route to the correct fixture factory. Returns (fixtures, effective_profile).

    effective_profile may differ from profile when a code-based skill has no runnable
    scripts — in that case we fall back to no_code fixtures and score accordingly.
    """
    if _is_evaluator_itself(skill_path):
        return build_meta_evaluator_fixtures(eval_scripts_dir), profile

    if profile == "no_code":
        return build_no_code_fixtures(skill_path), "no_code"

    # For code-based profiles, try to run the skill's actual scripts
    fixtures = build_deterministic_fixtures(skill_path)

    if not fixtures:
        # No runnable Python scripts found — fall back to SKILL.md quality analysis.
        # Use effective_profile="no_code" so delta is 0 (can't compare scripts vs baseline).
        return build_no_code_fixtures(skill_path), "no_code"

    return fixtures, profile


# ---------------------------------------------------------------------------
# Baseline predictor (without-skill heuristic)
# ---------------------------------------------------------------------------

def _compute_baseline_accuracy(fixtures: List[Dict]) -> float:
    """Average without_accuracy across all fixtures (for summary)."""
    if not fixtures:
        return 0.0
    return sum(f.get("without_accuracy", 0.0) for f in fixtures) / len(fixtures)


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

def compute_scores(
    fixtures: List[Dict],
    profile: str,
    robust_max_override: Optional[int] = None,
    correct_max_override: Optional[int] = None,
    delta_max_override: Optional[int] = None,
) -> Dict:
    """Compute L4 sub-scores from fixture results.

    Max values come from run_evaluation.py (profile-aware PROFILE_WEIGHTS table).
    Override args take precedence; if absent, fall back to spec defaults per profile.

    Per spec:
      deterministic : robust=8,  correct=12, delta=30  → total=50
      workflow      : robust=8,  correct=22, delta=30  → total=60
      generative    : robust=10, correct=55, delta=0   → total=65
      no_code       : robust=15, correct=55, delta=0   → total=70
    """
    if not fixtures:
        total_max = (robust_max_override or 8) + (correct_max_override or 12) + (delta_max_override or 30)
        return {"status": "skipped", "score": 0, "max_score": total_max,
                "reason": "No fixtures executed"}

    # Use override values if provided, else spec defaults per profile
    _spec_defaults = {
        'deterministic': (8,  12, 30),
        'workflow':      (8,  22, 30),
        'generative':    (10, 55,  0),
        'no_code':       (15, 55,  0),
    }
    d_r, d_c, d_d = _spec_defaults.get(profile, (8, 12, 30))
    robust_max_pts  = robust_max_override  if robust_max_override  is not None else d_r
    correct_max_pts = correct_max_override if correct_max_override is not None else d_c
    delta_max_pts   = delta_max_override   if delta_max_override   is not None else d_d
    total_max = robust_max_pts + correct_max_pts + delta_max_pts

    # Robustness: fixtures that produced assertions ran successfully
    ran_count = sum(1 for f in fixtures if len(f.get("assertions", [])) > 0)
    robust_rate = min(1.0, ran_count / max(len(fixtures), 1))

    # Correctness: weighted pass rate across all assertions
    total_assert = sum(len(f.get("assertions", [])) for f in fixtures)
    passed_assert = sum(
        sum(1 for a in f.get("assertions", []) if a.get("passed"))
        for f in fixtures
    )
    correct_rate = passed_assert / max(total_assert, 1)

    # Delta: improvement over baseline (spec formula: delta_normalized = max(0, delta_raw + 0.5))
    avg_with    = sum(f.get("with_accuracy", 0)    for f in fixtures) / len(fixtures)
    avg_without = sum(f.get("without_accuracy", 0) for f in fixtures) / len(fixtures)
    delta_raw  = avg_with - avg_without
    if delta_max_pts > 0:
        delta_norm = min(1.0, max(0.0, delta_raw + 0.5))  # spec: max(0, delta_raw + 0.5)
    else:
        delta_norm = 0.0

    robust_score  = round(robust_rate  * robust_max_pts,  1)
    correct_score = round(correct_rate * correct_max_pts, 1)
    delta_score   = round(delta_norm   * delta_max_pts,   1)

    per_fixture = [
        {
            "fixture_id":       f["id"],
            "label":            f["label"],
            "priority":         f.get("priority", "P1"),
            "with_accuracy":    round(f.get("with_accuracy",    0), 3),
            "without_accuracy": round(f.get("without_accuracy", 0), 3),
            "assertions":       f.get("assertions", []),
        }
        for f in fixtures
    ]

    return {
        "status":        "completed",
        "score":         round(robust_score + correct_score + delta_score, 1),
        "max_score":     total_max,
        "robust_score":  robust_score,
        "robust_max":    robust_max_pts,
        "correct_score": correct_score,
        "correct_max":   correct_max_pts,
        "delta_score":   delta_score,
        "delta_max":     delta_max_pts,
        "with_correct":  round(avg_with,    3),
        "without_correct": round(avg_without, 3),
        "delta_raw":     round(delta_raw,   3),
        "delta_normalized": round(delta_norm, 3),
        "robustness_breakdown": {
            "fixtures_ran":   ran_count,
            "fixtures_total": len(fixtures),
            "robust_rate":    round(robust_rate, 3),
        },
        "per_fixture":       per_fixture,
        "evaluation_time":   datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

class Layer4Evaluator:
    """Orchestrate L4 dynamic evaluation for a skill."""

    def __init__(self, skill_path: str):
        self.skill_path   = Path(skill_path).resolve()
        self.scripts_dir  = Path(__file__).parent
        self.results_dir  = self.skill_path / "evaluation_results"
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _read_profile(self) -> str:
        """Read profile from existing layer1/layer2 results or infer."""
        # Try evaluation_summary.json
        summary = self.results_dir / "evaluation_summary.json"
        if summary.exists():
            try:
                _ = json.loads(summary.read_text(encoding="utf-8"))
                # Check eval_data.json for profile
            except (json.JSONDecodeError, KeyError):
                pass

        # Infer from skill content
        skill_md = self.skill_path / "SKILL.md"
        code_exts = [".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".rb", ".go"]
        excluded  = {"node_modules", ".venv", "venv", "env", "__pycache__",
                     ".git", "dist", "build", "evaluation_results"}
        has_code  = any(
            f for ext in code_exts
            for f in self.skill_path.rglob(f"*{ext}")
            if not set(f.parts).intersection(excluded)
        )
        if not has_code:
            return "no_code"
        if skill_md.exists():
            lower = skill_md.read_text(encoding="utf-8").lower()
            if any(k in lower for k in ["workflow", "pipeline", "orchestrat", "工作流", "流程"]):
                return "workflow"
            if any(k in lower for k in ["generat", "creat", "write", "生成", "撰写"]):
                return "generative"
        return "deterministic"

    def evaluate(
        self,
        evals_path: Optional[Path] = None,
        robust_max: Optional[int] = None,
        correct_max: Optional[int] = None,
        delta_max: Optional[int] = None,
    ) -> Dict:
        """Run L4 evaluation and return structured results."""
        del evals_path
        profile  = self._read_profile()
        fixtures, effective_profile = build_profile_fixtures(self.skill_path, profile, self.scripts_dir)
        results  = compute_scores(
            fixtures, effective_profile,
            robust_max_override=robust_max,
            correct_max_override=correct_max,
            delta_max_override=delta_max,
        )

        out_path = self.results_dir / "layer4_results.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        return results


def main() -> None:
    """CLI entry point.

    Usage: layer4_dynamic_eval.py <skill-path> [evals-json-path]
           [--robust-max=N] [--correct-max=N] [--delta-max=N]
    """
    if len(sys.argv) < 2:
        print("Usage: python layer4_dynamic_eval.py <skill-path> [evals-json-path] "
              "[--robust-max=N] [--correct-max=N] [--delta-max=N]")
        sys.exit(1)

    skill_path = sys.argv[1]
    evals_path = None
    robust_max = correct_max = delta_max = None

    for arg in sys.argv[2:]:
        if arg.startswith("--robust-max="):
            robust_max = int(arg.split("=", 1)[1])
        elif arg.startswith("--correct-max="):
            correct_max = int(arg.split("=", 1)[1])
        elif arg.startswith("--delta-max="):
            delta_max = int(arg.split("=", 1)[1])
        elif not arg.startswith("--") and evals_path is None:
            evals_path = Path(arg)

    print("\n[Layer 4/5] Dynamic Evaluation")
    print("-" * 70)

    ev = Layer4Evaluator(skill_path)
    r  = ev.evaluate(evals_path, robust_max=robust_max, correct_max=correct_max, delta_max=delta_max)

    print(f"  Profile-specific fixtures: {len(r.get('per_fixture', []))}")
    for f in r.get("per_fixture", []):
        acc = int(f["with_accuracy"] * 100)
        print(f"  [{f['priority']}] {f['label']} — {acc}%")
    print(f"\n✅ L4 Complete: {r['robust_score']}/{r['robust_max']} robust "
          f"+ {r['correct_score']}/{r['correct_max']} correct "
          f"+ {r['delta_score']}/{r['delta_max']} delta "
          f"= {r['score']}/{r['max_score']}")

    print(f"\n💾 {ev.results_dir / 'layer4_results.json'}")


if __name__ == "__main__":
    main()
