"""Layer 1: Quick Screening — metadata, documentation, and basic compliance.

评分模式：打分制（不阻断评测）
- 每个检查项根据完成程度打分（0-100）
- 最终返回综合得分，不阻断后续评测
- 检查内容：
  - 元数据：name、description(≥20 字)、version、type、author、tags
  - 文档结构：Description、Parameters、Examples、Returns、ErrorHandling 章节
  - 基础合规：无可疑文件、无超大文件、无恶意命名、依赖声明
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

import structlog
import yaml

from evaluator.models.skill import SkillInfo

logger = structlog.get_logger()


class Layer1Screening:
    """Layer 1: Fast screening (<30s) to evaluate skill compliance.

    采用打分制：
    - 每个检查项返回 score: 0-100
    - 所有检查项综合计算最终得分
    - 不阻断后续评测，结果仅供参考
    """

    layer_number = 1
    layer_name = "layer1_screening"

    def __init__(self, skill_info: SkillInfo) -> None:
        self.skill_info = skill_info
        self.skill_path = skill_info.skill_path
        self.log = logger.bind(layer=self.layer_name, skill=skill_info.metadata.name)

    def run(self) -> dict:
        """Execute all Layer 1 checks.

        Returns:
            Result dict with score, checks, issues, duration_s.
            不阻断评测，始终返回结果。
        """
        t_start = time.monotonic()

        # 执行三类检查，返回分数
        meta_score, meta_issues, meta_items = self._check_metadata()
        doc_score, doc_issues, doc_items = self._check_documentation()
        comp_score, comp_issues, comp_items = self._check_basic_compliance()

        # 计算综合得分（三类检查的加权平均）
        # 元数据 30%，文档 40%，基础合规 30%
        total_score = round(meta_score * 0.3 + doc_score * 0.4 + comp_score * 0.3, 1)
        all_issues = meta_issues + doc_issues + comp_issues

        duration = round(time.monotonic() - t_start, 3)

        result = {
            "layer": 1,
            "score": total_score,
            "duration_s": duration,
            "checks": {
                "metadata": {
                    "score": meta_score,
                    "issues": meta_issues,
                    "items": meta_items,
                },
                "documentation": {
                    "score": doc_score,
                    "issues": doc_issues,
                    "items": doc_items,
                },
                "basic_compliance": {
                    "score": comp_score,
                    "issues": comp_issues,
                    "items": comp_items,
                },
            },
            "issues": all_issues,
            "summary": self._build_summary(meta_items, doc_items, comp_items),
        }

        self.log.info("layer1.complete", score=total_score, duration_s=duration)

        return result

    def _build_summary(self, meta_items: list, doc_items: list, comp_items: list) -> dict:
        """构建检查摘要统计。"""
        def count(items: list) -> dict:
            passed = sum(1 for i in items if i.get("passed", False))
            total = len(items)
            optional_passed = sum(1 for i in items if i.get("passed", False) and i.get("optional", False))
            optional_total = sum(1 for i in items if i.get("optional", False))
            return {
                "passed": passed,
                "total": total,
                "optional_passed": optional_passed,
                "optional_total": optional_total,
                "required_passed": passed - optional_passed,
                "required_total": total - optional_total,
            }

        def calc_score(items: list) -> float:
            """根据检查项通过情况计算分数。"""
            if not items:
                return 0
            # 必需项每项 100 分，可选项每项 50 分
            total_weight = sum(100 if not i.get("optional", False) else 50 for i in items)
            earned = sum(100 if i.get("passed", False) and not i.get("optional", False)
                         else 50 if i.get("passed", False) and i.get("optional", False)
                         else 0 for i in items)
            return round(earned / total_weight * 100, 1) if total_weight > 0 else 0

        return {
            "metadata": count(meta_items),
            "documentation": count(doc_items),
            "basic_compliance": count(comp_items),
            "scores": {
                "metadata": calc_score(meta_items),
                "documentation": calc_score(doc_items),
                "basic_compliance": calc_score(comp_items),
            }
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _read_skill_json(self) -> Optional[dict]:
        path = self.skill_path / "skill.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _read_frontmatter(self) -> tuple[str, dict]:
        """Return (body, frontmatter_dict) from SKILL.md."""
        skill_md = self.skill_path / "SKILL.md"
        if not skill_md.exists():
            return "", {}
        content = skill_md.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not m:
            return content, {}
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        return content[m.end():], fm

    def _make_item(self, label: str, passed: bool, detail: str, optional: bool = False) -> dict:
        """Build a single check-item record.

        Args:
            label: 检查项名称
            passed: 是否通过
            detail: 详细说明
            optional: 是否为可选项
        """
        return {
            "label": label,
            "passed": passed,
            "detail": detail,
            "optional": optional,
            "score": 100 if passed else 0,
        }

    def _check_metadata(self) -> tuple[float, list[str], list[dict]]:
        """检查元数据完整性。

        返回分数（0-100）：
        - name: kebab-case 格式（必需）
        - description: 至少 20 字（必需）
        - skill.json 文件（可选）
        - version（可选）
        - type（可选）
        - author（可选）
        - tags（可选）
        """
        issues: list[str] = []
        items: list[dict] = []

        skill_json = self._read_skill_json()
        _, fm = self._read_frontmatter()

        # 获取元数据的辅助函数
        def get_meta(key: str) -> tuple[str, str]:
            """从 skill.json 或 frontmatter 获取值，返回 (value, source)"""
            if skill_json and key in skill_json:
                return str(skill_json[key]), "skill.json"
            if key in fm:
                return str(fm[key]), "frontmatter"
            return "", ""

        # ── skill.json 文件（可选）──
        if skill_json is not None:
            items.append(self._make_item("skill.json 文件", True, "存在", optional=True))
        else:
            items.append(self._make_item("skill.json 文件", False, "不存在（可选，使用 frontmatter）", optional=True))

        # ── name（必须）──
        name_val, name_src = get_meta("name")
        if not name_val:
            issues.append("缺少 name 字段")
            items.append(self._make_item("名称 (name)", False, "缺失"))
        elif not re.match(r"^[a-z0-9-]+$", name_val):
            issues.append(f"name={name_val!r} 不符合 kebab-case 格式")
            items.append(self._make_item("名称 (name)", False, f"{name_val} (需 kebab-case)"))
        else:
            items.append(self._make_item("名称 (name)", True, f"{name_val} ({name_src})"))

        # ── version（可选）──
        ver_val, ver_src = get_meta("version")
        if ver_val:
            if re.match(r"^\d+\.\d+\.\d+", ver_val):
                items.append(self._make_item("版本号 (version)", True, f"{ver_val} ({ver_src})", optional=True))
            else:
                items.append(self._make_item("版本号 (version)", False, f"{ver_val} (建议 semver 格式)", optional=True))
        else:
            items.append(self._make_item("版本号 (version)", False, "缺失（可选）", optional=True))

        # ── type（可选）──
        type_val, type_src = get_meta("type")
        if type_val:
            if re.match(r"^(tool|analyzer|generator|workflow)$", type_val):
                items.append(self._make_item("类型 (type)", True, f"{type_val} ({type_src})", optional=True))
            else:
                items.append(self._make_item("类型 (type)", False, f"{type_val} (建议 tool/analyzer/generator/workflow)", optional=True))
        else:
            items.append(self._make_item("类型 (type)", False, "缺失（可选，将自动推断）", optional=True))

        # ── description（必须）──
        desc_val, desc_src = get_meta("description")
        if len(desc_val) < 20:
            issues.append(f"description 太短 ({len(desc_val)} 字，需 ≥20)")
            items.append(self._make_item("描述 (description)", False, f"{len(desc_val)} 字，需 ≥20"))
        else:
            items.append(self._make_item("描述 (description)", True, f"{len(desc_val)} 字 ({desc_src})"))

        # ── author（可选）──
        author_val, author_src = get_meta("author")
        if author_val:
            items.append(self._make_item("作者 (author)", True, f"{author_val[:40]} ({author_src})", optional=True))
        else:
            items.append(self._make_item("作者 (author)", False, "缺失（可选）", optional=True))

        # ── tags（可选）──
        tags_val, tags_src = get_meta("tags")
        if tags_val:
            # tags 可能是字符串或列表
            if isinstance(tags_val, str):
                tags_list = [t.strip() for t in tags_val.split(",") if t.strip()]
            else:
                tags_list = list(tags_val) if isinstance(tags_val, list) else []
            if tags_list:
                items.append(self._make_item("标签 (tags)", True, f"{', '.join(str(t) for t in tags_list[:4])} ({tags_src})", optional=True))
            else:
                items.append(self._make_item("标签 (tags)", False, "空（可选）", optional=True))
        else:
            items.append(self._make_item("标签 (tags)", False, "缺失（可选）", optional=True))

        # 计算分数
        score = self._calc_score(items)
        return score, issues, items

    def _calc_score(self, items: list[dict]) -> float:
        """根据检查项通过情况计算分数。"""
        if not items:
            return 0
        # 必需项每项 100 分，可选项每项 50 分
        total_weight = sum(100 if not i.get("optional", False) else 50 for i in items)
        earned = sum(100 if i.get("passed", False) and not i.get("optional", False)
                     else 50 if i.get("passed", False) and i.get("optional", False)
                     else 0 for i in items)
        return round(earned / total_weight * 100, 1) if total_weight > 0 else 0

    def _check_documentation(self) -> tuple[float, list[str], list[dict]]:
        """检查文档结构完整性。

        返回分数（0-100）：
        - SKILL.md 文件存在
        - Description/概述章节
        - Parameters/参数章节（含类型/必填/说明列）
        - Examples/示例章节（含输入/输出对）
        - Returns/输出章节
        - Error handling/注意事项章节
        """
        issues: list[str] = []
        items: list[dict] = []

        _is_behavioral = False  # 可以后续根据 profile 判断

        skill_md = self.skill_path / "SKILL.md"
        if not skill_md.exists():
            issues.append("SKILL.md 文件不存在")
            items.append(self._make_item("SKILL.md 主文档存在", False, "文件不存在"))
            return 0, issues, items

        items.append(self._make_item("SKILL.md 主文档存在", True, "文件存在"))
        content = skill_md.read_text(encoding="utf-8")
        content_lower = content.lower()

        # Description section
        _desc_pattern = (
            r"##\s*("
            r"description|概述 | 简介|overview"
            r"|场景路由 | 核心工作流 | 工作流 | 功能介绍 | 功能说明 | 使用说明 | 使用指南"
            r"|目录 | 简要说明 |skill\s*简介 | 介绍|about|简述"
            r"|核心行为 | 行为协议 | 核心协议 | 使用协议"
            r"|what\s+is|introduction|背景|purpose"
            r")"
        )
        if not re.search(_desc_pattern, content_lower):
            issues.append("缺少 ## Description 章节")
            items.append(self._make_item("概述/描述章节 (Description)", False, "未找到该章节"))
        else:
            items.append(self._make_item("概述/描述章节 (Description)", True, "章节存在"))

        # Parameters section
        param_m = re.search(r"##\s*(?:parameters?|参数)(.*?)(?=\n##(?!#)|\Z)", content_lower, re.DOTALL)
        if not param_m:
            issues.append("缺少 ## Parameters 章节")
            items.append(self._make_item("参数章节含类型/必填说明", False, "未找到参数章节"))
        else:
            param_body = param_m.group(1)
            has_table = "|" in param_body
            missing_cols = [c for c in ["type|类型", "required|必填", "description|说明"] if not re.search(c, param_body)]
            if missing_cols:
                issues.append(f"Parameters 表格缺少列：{', '.join(missing_cols[:3])}")
                items.append(self._make_item("参数章节含类型/必填说明", False,
                                             f"{'有表格' if has_table else '无表格'}，缺少列：{missing_cols[:2]}"))
            else:
                items.append(self._make_item("参数章节含类型/必填说明", True,
                                             "有表格，包含类型/必填/说明列"))

        # Examples section
        _examples_pattern = (
            r"##\s*(?:examples?|示例 | 使用示例"
            r"|running|how\s+to|usage|getting\s+started|quick\s+start"
            r"|使用方法 | 操作步骤 | 用法|workflow|演示|demo"
            r"|running\s+and\s+evaluating|test\s+cases?)"
        )
        ex_m = re.search(_examples_pattern, content_lower)
        if not ex_m:
            issues.append("缺少 ## Examples 章节")
            items.append(self._make_item("Examples 章节 + 输入输出对", False, "missing"))
        else:
            ex_body_m = re.search(
                r"##\s*(?:examples?|示例 | 使用示例 |running|how\s+to|usage|getting\s+started"
                r"|使用方法 | 操作步骤 | 用法|workflow|演示|demo|running\s+and\s+evaluating|test\s+cases?)"
                r"(.*?)(?=\n##(?!#)|\Z)",
                content_lower, re.DOTALL,
            )
            ex_body = ex_body_m.group(1) if ex_body_m else ""
            has_io = bool(re.search(r"(```|输入 | 输出|input|output|prompt|result|run|execute)", ex_body, re.IGNORECASE))
            if not has_io:
                issues.append("Examples 章节缺少输入/输出示例对")
                items.append(self._make_item("使用示例章节（含输入/输出对）", False, "缺少 I/O 示例对"))
            else:
                items.append(self._make_item("使用示例章节（含输入/输出对）", True, "有示例章节和 I/O 对"))

        # Returns section
        has_returns = bool(re.search(
            r"##\s*(returns?|输出|output|返回 | 输出说明 | 返回值)",
            content_lower,
        ))
        if not has_returns:
            issues.append("缺少 ## Returns 章节")
            items.append(self._make_item("返回值/输出说明章节", False, "未找到返回值章节"))
        else:
            items.append(self._make_item("返回值/输出说明章节", True, "章节存在"))

        # Error handling / constraints section
        has_error = bool(re.search(
            r"##\s*(error|错误|exception|限制 |limitations?|阻断 | 注意事项"
            r"|质量检查 | 检查清单|check|checklist|红线 | 约束 | 边界|gotcha"
            r"|recommendations?|建议 | 注意|warning|known\s+issues?|caveats?)",
            content_lower,
        ))
        if not has_error:
            issues.append("缺少错误处理/注意事项章节")
            items.append(self._make_item("错误处理/注意事项章节", False, "未找到相关章节"))
        else:
            items.append(self._make_item("错误处理/注意事项章节", True, "章节存在"))

        # 计算分数
        score = self._calc_score(items)
        return score, issues, items

    def _check_basic_compliance(self) -> tuple[float, list[str], list[dict]]:
        """检查基础合规性。

        返回分数（0-100）：
        - 无可疑可执行文件 (.exe/.bat/.vbs)
        - 无超大文件 (>10MB)
        - 无恶意文件名关键字
        - 依赖声明文件（有代码时必需）
        """
        issues: list[str] = []
        items: list[dict] = []

        suspicious_exts = {".exe", ".bat", ".cmd", ".vbs"}
        dep_files = {"requirements.txt", "package.json", "pom.xml", "pyproject.toml",
                     "go.mod", "cargo.toml", "gemfile", "composer.json"}
        max_file_size = 10 * 1024 * 1024  # 10 MB
        found_exec = False
        found_large = False
        found_suspect_name = False
        found_dep_file = False
        has_code = self.skill_info.has_code

        for file_path in self.skill_path.rglob("*"):
            if not file_path.is_file():
                continue
            rel = str(file_path.relative_to(self.skill_path))
            if any(part in rel for part in (".git/", "storage/", "__pycache__")):
                continue

            # Dependency declaration
            if file_path.name.lower() in dep_files:
                found_dep_file = True

            if file_path.suffix.lower() in suspicious_exts and not found_exec:
                issues.append(f"可疑可执行文件：{rel}")
                found_exec = True

            if file_path.stat().st_size > max_file_size and not found_large:
                mb = file_path.stat().st_size / (1024 * 1024)
                issues.append(f"文件过大：{rel} ({mb:.1f}MB > 10MB)")
                found_large = True

            name_lower = file_path.name.lower()
            if re.search(r"(malware|virus|hack|crack|keylogger|backdoor)", name_lower) and not found_suspect_name:
                issues.append(f"可疑文件名：{rel}")
                found_suspect_name = True

        # Dependency declaration: only required when has_code=True
        dep_ok = (not has_code) or found_dep_file
        if not dep_ok:
            issues.append("有代码文件但缺少依赖声明文件 (requirements.txt/package.json 等)")

        items.append(self._make_item("无可疑可执行文件 (.exe/.bat/.vbs)", not found_exec,
                                     "发现可疑文件" if found_exec else "无可疑文件"))
        items.append(self._make_item("无超大文件 (>10MB)", not found_large,
                                     "有超大文件" if found_large else "所有文件大小正常"))
        items.append(self._make_item("无恶意文件名关键字", not found_suspect_name,
                                     "发现可疑文件名" if found_suspect_name else "文件名正常"))
        items.append(self._make_item(
            "依赖声明文件 (requirements.txt 等)" if has_code else "依赖声明（无代码文件，自动通过）",
            dep_ok,
            "已找到依赖文件" if found_dep_file else ("无代码，跳过" if not has_code else "缺少依赖声明文件"),
        ))

        # 计算分数
        score = self._calc_score(items)
        return score, issues, items