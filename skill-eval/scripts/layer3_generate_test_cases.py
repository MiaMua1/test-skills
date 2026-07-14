#!/usr/bin/env python3
"""
Layer 3: Test Case Generation
Automatically generate test cases from SKILL.md with quantifiable evaluation criteria.
Archives every generated test suite with a timestamp.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import yaml


class TestCaseGenerator:
    """Generate test cases from SKILL.md with quantifiable evaluation criteria."""

    def __init__(self, skill_path: str):
        self.skill_path = Path(skill_path)
        self.skill_md_path = self.skill_path / "SKILL.md"
        self.evals_dir = self.skill_path / "evals"
        self.evals_path = self.evals_dir / "evals.json"
        self.archive_dir = self.evals_dir / "archive"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, force: bool = False) -> Dict:
        """Generate test cases from SKILL.md.

        Args:
            force: If True, regenerate even if evals.json already exists.

        Returns:
            Full test-case structure (also written to evals.json + archive).
        """
        if self.evals_path.exists() and not force:
            print(f"Test cases already exist at {self.evals_path}")
            print("Loading existing test cases...")
            with open(self.evals_path, "r", encoding="utf-8") as f:
                return json.load(f)

        if not self.skill_md_path.exists():
            return {"error": "SKILL.md not found", "skill_path": str(self.skill_path)}

        content = self.skill_md_path.read_text(encoding="utf-8")

        metadata = self._extract_metadata(content)
        examples = self._extract_examples(content)
        behaviors = self._extract_behaviors(content)
        mcp_tools = self._extract_mcp_tools(content)
        output_promises = self._extract_output_promises(content)

        test_cases = self._build_test_cases(metadata, examples, behaviors, output_promises)

        evals_structure = {
            "skill_name": metadata.get("name", self.skill_path.name),
            "skill_version": metadata.get("version", ""),
            "generated_from": "SKILL.md",
            "generation_time": datetime.now().isoformat(),
            "mcp_tools_required": mcp_tools,
            "test_cases": test_cases,
            "coverage": {
                "happy_path": len([t for t in test_cases if t["priority"] == "P0"]),
                "edge_cases": len([t for t in test_cases if t["priority"] == "P1"]),
                "error_cases": len([t for t in test_cases if t["priority"] == "P2"]),
                "total": len(test_cases),
            },
        }

        self._save(evals_structure)
        self._archive(evals_structure)

        print(f"\nGenerated {len(test_cases)} test cases")
        print(f"Saved to:  {self.evals_path}")
        print(f"Archived:  {self.archive_dir}/")

        return evals_structure

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_metadata(self, content: str) -> Dict:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if match:
            try:
                return yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError:
                return {}
        return {}

    def _extract_examples(self, content: str) -> List[Dict]:
        examples = []
        for pattern in [
            r"(?i)###?\s*example[s]?[:\s]*(.*?)(?=###?|\Z)",
            r"(?i)###?\s*usage[:\s]*(.*?)(?=###?|\Z)",
            r"(?i)###?\s*场景[:\s]*(.*?)(?=###?|\Z)",
        ]:
            for match in re.finditer(pattern, content, re.DOTALL):
                text = match.group(1)
                for code in re.findall(r"```(?:\w+)?\s*\n(.*?)\n```", text, re.DOTALL):
                    examples.append({"type": "code_example", "content": code.strip()})
        return examples

    def _extract_behaviors(self, content: str) -> List[str]:
        behaviors = []
        for pattern in [
            r"expected output[:\s]+([^\n]+)",
            r"should (?:return|produce|generate|output)[:\s]+([^\n]+)",
            r"输出[：:]\s*([^\n]+)",
            r"生成[：:]\s*([^\n]+)",
        ]:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                behaviors.append(match.group(1).strip())
        return behaviors

    def _extract_mcp_tools(self, content: str) -> List[str]:
        """Extract MCP tool names mentioned in the skill."""
        known_tools = [
            "skylark_user_doc_detail", "skylark_user_doc_create",
            "query_api", "query_api_rag", "get_api_list",
        ]
        found = [t for t in known_tools if t in content]
        # Also detect generic CallMcpTool usage
        if "CallMcpTool" in content or "call_mcp" in content.lower():
            found.append("CallMcpTool (generic)")
        return list(dict.fromkeys(found))

    def _extract_output_promises(self, content: str) -> List[str]:
        """Extract output section names from the skill (for completeness criteria)."""
        sections = []
        # Look for numbered section headers like "# 1、" or "## 1.1" or "### Phase 2"
        for m in re.finditer(r"^#{1,3}\s+(.+)", content, re.MULTILINE):
            title = m.group(1).strip()
            if len(title) > 3 and len(title) < 80:
                sections.append(title)
        return sections[:20]  # cap to 20

    # ------------------------------------------------------------------
    # Test case builder
    # ------------------------------------------------------------------

    def _build_test_cases(
        self,
        metadata: Dict,
        examples: List[Dict],
        behaviors: List[str],
        output_promises: List[str],
    ) -> List[Dict]:
        """Generate skill-specific test cases derived from the actual SKILL.md content.

        Each case must have a REAL prompt grounded in the skill's description,
        trigger phrases, examples and optional features — not generic templates.
        """
        del behaviors
        test_cases: List[Dict] = []
        # --- Extract skill identity ---
        skill_name = metadata.get("name", "") or self.skill_path.name
        description = str(metadata.get("description", ""))
        skill_type = metadata.get("type", "")

        # --- Parse full SKILL.md for richer context ---
        full_content = ""
        if self.skill_md_path.exists():
            full_content = self.skill_md_path.read_text(encoding="utf-8")

        # Extract trigger phrases embedded in description (things after 触发关键词: or 适用于:)
        trigger_phrases = self._extract_trigger_phrases(description, full_content)
        # Extract what the skill takes as inputs
        input_hints = self._extract_input_hints(full_content, description)
        # Extract what it produces as outputs
        output_hints = self._extract_output_hints(full_content, description)
        # Extract optional features / conditional steps
        optional_features = self._extract_optional_features(full_content, description)

        display_name = skill_name.replace("-", " ")
        idx = 1

        # ==== P0: Core happy-path cases ====

        # P0-1: Primary use case from trigger phrase or description
        primary_prompt = self._make_primary_prompt(
            trigger_phrases, description, display_name, input_hints, output_hints
        )
        test_cases.append(self._make_test_case(
            tc_id=f"tc_{idx:03d}",
            source="primary_use_case",
            priority="P0",
            prompt=primary_prompt,
            expected_behavior=f"成功完成核心功能，输出符合描述：{output_hints[0] if output_hints else '预期产出'}",
            key_checks=["completion", "no_errors", "structure"],
            output_promises=output_promises,
            skill_name=display_name,
        ))
        idx += 1

        # P0-2: Full workflow / complete input case (for multi-step skills)
        is_workflow = (skill_type == "workflow" or
                       any(kw in description.lower() for kw in
                           ["workflow", "工作流", "pipeline", "流程", "多步", "步骤"]))
        if is_workflow or len(output_hints) > 2:
            full_input_prompt = self._make_full_workflow_prompt(
                trigger_phrases, description, display_name, input_hints, output_hints
            )
            test_cases.append(self._make_test_case(
                tc_id=f"tc_{idx:03d}",
                source="full_workflow",
                priority="P0",
                prompt=full_input_prompt,
                expected_behavior="全部步骤顺序执行完成，输出结构完整，包含所有声明的产出部分",
                key_checks=["completion", "structure", "no_hallucination"],
                output_promises=output_promises,
                skill_name=display_name,
            ))
            idx += 1

        # P0-3: Example-based case (if examples present)
        for example in examples[:1]:
            preview = example["content"][:120].replace("\n", " ")
            test_cases.append(self._make_test_case(
                tc_id=f"tc_{idx:03d}",
                source="from_example",
                priority="P0",
                prompt=f"按照文档示例执行：{preview}",
                expected_behavior="按照示例执行成功，输出与示例描述一致",
                key_checks=["completion", "format"],
                output_promises=output_promises,
                skill_name=display_name,
            ))
            idx += 1
            break

        # ==== P1: Edge cases ====

        # P1-1: Minimal input — skill receives only the bare minimum
        minimal_prompt = self._make_minimal_input_prompt(
            description, display_name, input_hints, trigger_phrases
        )
        test_cases.append(self._make_test_case(
            tc_id=f"tc_{idx:03d}",
            source="edge_minimal_input",
            priority="P1",
            prompt=minimal_prompt,
            expected_behavior="对最简输入友好响应：要么完成基础输出，要么明确询问缺少的信息",
            key_checks=["completion", "graceful_handling"],
            output_promises=[],
            skill_name=display_name,
        ))
        idx += 1

        # P1-2: Optional feature omitted — tests conditional branching
        if optional_features:
            skip_desc = optional_features[0]
            test_cases.append(self._make_test_case(
                tc_id=f"tc_{idx:03d}",
                source="optional_feature_skip",
                priority="P1",
                prompt=self._make_optional_skip_prompt(description, display_name, skip_desc, input_hints),
                expected_behavior=f"正确跳过可选功能「{skip_desc[:30]}」，仍能生成基础输出，不报错",
                key_checks=["completion", "conditional_skip"],
                output_promises=[],
                skill_name=display_name,
            ))
            idx += 1

        # P1-3: Secondary trigger / alternative use case
        if len(trigger_phrases) > 1:
            alt_prompt = self._make_secondary_prompt(trigger_phrases[1], description, display_name)
            test_cases.append(self._make_test_case(
                tc_id=f"tc_{idx:03d}",
                source="secondary_trigger",
                priority="P1",
                prompt=alt_prompt,
                expected_behavior="处理另一类典型触发场景，输出符合对应场景要求",
                key_checks=["completion", "no_errors"],
                output_promises=[],
                skill_name=display_name,
            ))
            idx += 1

        # ==== P2: Error / boundary cases ====

        # P2-1: Missing or invalid input
        test_cases.append(self._make_test_case(
            tc_id=f"tc_{idx:03d}",
            source="invalid_input",
            priority="P2",
            prompt=self._make_invalid_input_prompt(description, display_name, input_hints),
            expected_behavior="输出明确的错误提示或请求澄清，不崩溃，不产生幻觉内容",
            key_checks=["no_crash", "error_message", "no_hallucination"],
            output_promises=[],
            skill_name=display_name,
        ))
        idx += 1

        return test_cases

    # ------------------------------------------------------------------
    # Skill-specific prompt builders
    # ------------------------------------------------------------------

    def _extract_trigger_phrases(self, description: str, full_content: str) -> List[str]:
        """Extract concrete trigger phrases from description and SKILL.md content."""
        phrases: List[str] = []

        # From description: extract phrases after 触发关键词/支持/适用 etc.
        for pattern in [
            r"触发关键词[：:]\s*「?([^」\n，,。]+)",
            r"当用户[需要]?(.{5,40}?)时",
            r"支持(.{3,40}?)(?:[，,。\n]|$)",
            r"Use when[:\s]+(.{5,60}?)(?:[.,\n]|$)",
            r"Triggers? on[:\s]+(.{5,60}?)(?:[.,\n]|$)",
        ]:
            for m in re.finditer(pattern, description, re.IGNORECASE):
                phrase = m.group(1).strip().rstrip("，,。")
                if 3 < len(phrase) < 60:
                    phrases.append(phrase)
                if len(phrases) >= 5:
                    break

        # Also scan SKILL.md body for bullet-point trigger examples
        for m in re.finditer(
            r"(?:^|\n)[•\-*]\s*[「「'\"「]([^」'\"\n「]{5,80})[」'\"\n「]",
            full_content, re.MULTILINE
        ):
            phrases.append(m.group(1).strip())
            if len(phrases) >= 8:
                break

        return list(dict.fromkeys(phrases))  # deduplicate, preserve order

    def _extract_input_hints(self, full_content: str, description: str) -> List[str]:
        """Extract what kinds of inputs the skill expects."""
        hints: List[str] = []
        combined = description + "\n" + full_content
        for pattern in [
            r"(?i)输入[：:]\s*([^\n，,]{5,60})",
            r"(?i)需要提供[：:]\s*([^\n，,]{5,60})",
            r"(?i)based on[:\s]+([^\n,]{5,60})",
            r"(?i)Input[s]?[:\s]+([^\n,]{5,60})",
            r"(?i)分析(.{5,40}?)(?:文档|材料|内容|信息)",
            r"(?i)根据(.{5,40}?)(?:文档|材料|内容)",
        ]:
            for m in re.finditer(pattern, combined):
                hint = m.group(1).strip().rstrip("。，,")
                if 3 < len(hint) < 60 and hint not in hints:
                    hints.append(hint)
                    if len(hints) >= 5:
                        break
        return hints

    def _extract_output_hints(self, full_content: str, description: str) -> List[str]:
        """Extract what the skill produces."""
        hints: List[str] = []
        combined = description + "\n" + full_content
        for pattern in [
            r"(?i)生成(.{5,40}?)(?:[，,。\n]|$)",
            r"(?i)输出(.{5,40}?)(?:[，,。\n]|$)",
            r"(?i)produces?[:\s]+([^\n,]{5,60})",
            r"(?i)generates?[:\s]+([^\n,]{5,60})",
            r"(?i)撰写(.{5,40}?)(?:[，,。\n]|$)",
        ]:
            for m in re.finditer(pattern, combined):
                hint = m.group(1).strip().rstrip("，,。")
                if 3 < len(hint) < 60 and hint not in hints:
                    hints.append(hint)
                    if len(hints) >= 5:
                        break
        return hints

    def _extract_optional_features(self, full_content: str, description: str) -> List[str]:
        """Extract optional/conditional features mentioned in the skill."""
        features: List[str] = []
        combined = description + "\n" + full_content
        for pattern in [
            r"(?i)(?:可选|optional)[：:\s]+([^\n，,。]{5,50})",
            r"(?i)支持(.{5,40}?)(?:集成|查询|发布|接入)",
            r"(?i)如果.*提供(.{5,40}?)则",
            r"(?i)when.*available[,:\s]+(.{5,60})",
        ]:
            for m in re.finditer(pattern, combined):
                feat = m.group(1).strip().rstrip("，,。")
                if 3 < len(feat) < 60 and feat not in features:
                    features.append(feat)
                    if len(features) >= 3:
                        break
        return features

    def _make_primary_prompt(
        self,
        trigger_phrases: List[str],
        description: str,
        display_name: str,
        input_hints: List[str],
        output_hints: List[str],
    ) -> str:
        """Build a realistic primary-use-case prompt from extracted content."""
        # Use first trigger phrase if available and actionable
        if trigger_phrases:
            p = trigger_phrases[0]
            # If it's already a complete sentence, use it as-is
            if len(p) > 20 and any(c in p for c in "生成撰写分析创建检查评测"):
                return p
            if len(p) > 20 and re.search(r"[a-zA-Z]", p):
                return p

        # Synthesize from input + output hints
        if input_hints and output_hints:
            return f"请根据以下{input_hints[0]}，{output_hints[0]}"
        if output_hints:
            return f"请{output_hints[0]}"
        if input_hints:
            return f"分析以下{input_hints[0]}，完成核心任务"

        # Last resort: paraphrase description action verb
        desc_lower = description.lower()
        if "生成" in desc_lower or "撰写" in desc_lower:
            subject = re.search(r"(生成|撰写)(.{5,30}?)(?:[，,。\n]|$)", description)
            if subject:
                return f"请{subject.group(0).rstrip('，,。')}"
        if "分析" in desc_lower or "检测" in desc_lower:
            return f"请对以下内容进行分析，使用 {display_name} 完成检测"
        if any(w in desc_lower for w in ["create", "generate", "write"]):
            return f"Use {display_name} to create a new item with standard configuration"
        return f"请完整演示 {display_name} 的核心功能，提供典型输入"

    def _make_full_workflow_prompt(
        self,
        trigger_phrases: List[str],
        description: str,
        display_name: str,
        input_hints: List[str],
        output_hints: List[str],
    ) -> str:
        del description
        if len(trigger_phrases) > 1:
            return trigger_phrases[1]
        if input_hints and output_hints and len(output_hints) > 1:
            return (f"提供完整的{input_hints[0]}，"
                    f"要求{display_name}完整执行所有步骤，"
                    f"最终输出包含{output_hints[0]}和{output_hints[1]}")
        return (f"提供完整输入材料，端到端运行 {display_name} 全流程，"
                f"验证所有步骤均按文档顺序执行，输出结构完整")

    def _make_minimal_input_prompt(
        self,
        description: str,
        display_name: str,
        input_hints: List[str],
        trigger_phrases: List[str],
    ) -> str:
        del description
        if input_hints:
            return (f"只提供{input_hints[0]}的基本名称，"
                    f"不附带详细说明，测试 {display_name} 如何处理最少信息")
        if trigger_phrases:
            phrase = trigger_phrases[-1] if trigger_phrases else ""
            return f"{phrase}（仅提供最少必要信息，其余留空）"
        return f"仅输入一句话描述需求，测试 {display_name} 对最少输入的响应"

    def _make_optional_skip_prompt(
        self,
        description: str,
        display_name: str,
        optional_feature: str,
        input_hints: List[str],
    ) -> str:
        del description
        base = input_hints[0] if input_hints else "基础内容"
        return (f"提供{base}，但不提供{optional_feature}相关信息，"
                f"测试 {display_name} 是否能跳过该可选功能并正常完成基础输出")

    def _make_secondary_prompt(
        self, trigger: str, description: str, display_name: str
    ) -> str:
        del description
        if len(trigger) > 15:
            return trigger
        return f"触发 {display_name} 的另一类典型场景：{trigger}"

    def _make_invalid_input_prompt(
        self, description: str, display_name: str, input_hints: List[str]
    ) -> str:
        del description
        if input_hints:
            return (f"提供明显无效的{input_hints[0]}（例如空内容、乱码或完全不相关的格式），"
                    f"观察 {display_name} 的错误处理行为")
        return (f"向 {display_name} 提供空输入或格式完全错误的数据，"
                f"验证错误处理是否友好")

    # ------------------------------------------------------------------
    # Test case factory
    # ------------------------------------------------------------------

    def _make_test_case(
        self,
        tc_id: str,
        source: str,
        priority: str,
        prompt: str,
        expected_behavior: str,
        key_checks: List[str],
        output_promises: List[str],
        skill_name: str,
    ) -> Dict:
        del skill_name
        assertions = self._build_assertions(key_checks, source)
        criteria = self._build_criteria(key_checks, output_promises, source)
        token_budget = self._estimate_token_budget(priority, output_promises)

        return {
            "id": tc_id,
            "source": source,
            "priority": priority,
            "prompt": prompt,
            "input_files": [],
            "expected_behavior": expected_behavior,
            "assertions": assertions,
            "evaluation_criteria": criteria,
            "token_budget": token_budget,
        }

    def _build_assertions(self, key_checks: List[str], source: str = "") -> List[Dict]:
        """Build binary assertions matching the specific test case type.

        Each source/check type gets a distinct assertion set — not the same list recycled.
        """
        assertions = []

        # ── Error / boundary cases ──────────────────────────────────────────
        if source in ("invalid_input", "error_invalid_input"):
            # P2: we WANT the skill to output an error hint, NOT a normal result
            assertions.append({
                "name": "no_fatal_crash",
                "description": "无致命崩溃或未捕获异常（进程应正常结束）",
                "type": "not_contains",
                "pattern": r"(?i)(fatal error|segfault|killed|abort|uncaught exception)",
            })
            assertions.append({
                "name": "has_error_hint",
                "description": "对无效输入给出可读的错误提示或请求澄清",
                "type": "contains_any",
                "patterns": [r"(?i)(invalid|error|cannot|unable|不支持|请提供|缺少|需要|无法|格式错误)"],
            })
            assertions.append({
                "name": "no_hallucinated_result",
                "description": "不应在无效输入上伪造正常结果",
                "type": "not_contains",
                "pattern": r"(?i)(成功|完成|generated|已生成|输出如下)",
            })
            return assertions

        # ── Edge case: minimal input ─────────────────────────────────────────
        if source == "edge_minimal_input":
            assertions.append({
                "name": "no_fatal_crash",
                "description": "最少输入下不崩溃",
                "type": "not_contains",
                "pattern": r"(?i)(fatal|segfault|killed|abort)",
            })
            assertions.append({
                "name": "has_any_response",
                "description": "有任何非空响应（输出结果 OR 询问更多信息）",
                "type": "min_length",
                "min_chars": 10,
            })
            assertions.append({
                "name": "no_silent_failure",
                "description": "不静默失败（有明确响应内容）",
                "type": "not_contains",
                "pattern": r"^\s*$",
            })
            return assertions

        # ── Edge case: optional feature skipped ──────────────────────────────
        if source == "optional_feature_skip":
            assertions.append({
                "name": "no_fatal_crash",
                "description": "跳过可选功能后不崩溃",
                "type": "not_contains",
                "pattern": r"(?i)(fatal|segfault|killed|abort)",
            })
            assertions.append({
                "name": "has_basic_output",
                "description": "即使跳过可选功能，仍有基础输出",
                "type": "min_length",
                "min_chars": 50,
            })
            assertions.append({
                "name": "no_missing_param_crash",
                "description": "不因缺少可选参数而报错终止",
                "type": "not_contains",
                "pattern": r"(?i)(KeyError|required.*missing|parameter.*required|AttributeError)",
            })
            return assertions

        # ── Happy-path: primary use case ────────────────────────────────────
        if source == "primary_use_case":
            assertions.append({
                "name": "no_errors",
                "description": "输出中无错误/异常信息",
                "type": "not_contains",
                "pattern": r"(?i)(traceback|exception|failed|error:)",
            })
            assertions.append({
                "name": "non_empty_output",
                "description": "输出内容非空（核心功能有实质性输出）",
                "type": "min_length",
                "min_chars": 100,
            })
            assertions.append({
                "name": "no_placeholder_content",
                "description": "不含占位符或明显幻觉内容",
                "type": "not_contains",
                "pattern": r"YOUR-USERNAME|YOUR-REPO|placeholder|TODO: fill",
            })
            return assertions

        # ── Happy-path: full workflow ─────────────────────────────────────────
        if source == "full_workflow":
            assertions.append({
                "name": "no_errors",
                "description": "全流程执行无错误/异常",
                "type": "not_contains",
                "pattern": r"(?i)(traceback|exception|failed|error:)",
            })
            assertions.append({
                "name": "substantial_output",
                "description": "全流程有充分的输出内容",
                "type": "min_length",
                "min_chars": 200,
            })
            assertions.append({
                "name": "no_placeholder_content",
                "description": "不含占位符或幻觉内容",
                "type": "not_contains",
                "pattern": r"YOUR-USERNAME|YOUR-REPO|placeholder|TODO: fill",
            })
            return assertions

        # ── Happy-path: from example ──────────────────────────────────────────
        if source == "from_example":
            assertions.append({
                "name": "no_errors",
                "description": "执行示例时无错误",
                "type": "not_contains",
                "pattern": r"(?i)(traceback|exception|failed|error:)",
            })
            assertions.append({
                "name": "non_empty_output",
                "description": "示例输出非空",
                "type": "min_length",
                "min_chars": 50,
            })
            return assertions

        # ── Secondary trigger ─────────────────────────────────────────────────
        if source == "secondary_trigger":
            assertions.append({
                "name": "no_errors",
                "description": "处理另一类触发词时无错误",
                "type": "not_contains",
                "pattern": r"(?i)(traceback|exception|failed|error:)",
            })
            assertions.append({
                "name": "non_empty_output",
                "description": "有实质性输出",
                "type": "min_length",
                "min_chars": 50,
            })
            return assertions

        # ── Fallback: use key_checks for any other source ─────────────────────
        if "completion" in key_checks:
            assertions.append({
                "name": "no_errors",
                "description": "输出中无错误/异常信息",
                "type": "not_contains",
                "pattern": r"(?i)(traceback|exception|failed|error:)",
            })
            assertions.append({
                "name": "non_empty_output",
                "description": "输出内容非空",
                "type": "min_length",
                "min_chars": 50,
            })
        if "no_crash" in key_checks:
            assertions.append({
                "name": "no_fatal_crash",
                "description": "无致命崩溃",
                "type": "not_contains",
                "pattern": r"(?i)(fatal|segfault|killed|abort)",
            })
        if "no_hallucination" in key_checks:
            assertions.append({
                "name": "no_placeholder_content",
                "description": "不含占位符或明显幻觉内容",
                "type": "not_contains",
                "pattern": r"YOUR-USERNAME|YOUR-REPO|placeholder|TODO: fill",
            })
        if "error_message" in key_checks:
            assertions.append({
                "name": "has_error_hint",
                "description": "无效输入时提供可读提示",
                "type": "contains_any",
                "patterns": [r"(?i)(invalid|error|cannot|unable|请提供|缺少|需要)"],
            })
        return assertions

    def _build_criteria(
        self, key_checks: List[str], output_promises: List[str], source: str = ""
    ) -> List[Dict]:
        """Build evaluation criteria tailored to the test case type.

        Error/boundary cases get error-handling criteria, NOT output-completeness.
        Happy-path cases get output quality + completeness.
        """
        criteria = []

        # ── P2 error case: evaluate error handling, NOT output completeness ──
        if source in ("invalid_input", "error_invalid_input"):
            criteria.append({
                "criterion": "error_handling_quality",
                "description": "错误处理质量：是否给出清晰提示而非崩溃或伪造结果",
                "method": "assess_error_response",
                "weight": 0.60,
                "scoring": {
                    "full_score": "明确提示输入无效，说明期望的格式，不产生幻觉结果",
                    "partial_score": "有提示但描述模糊，或有少量不相关内容",
                    "zero_score": "崩溃、静默失败，或伪造了正常结果",
                },
            })
            criteria.append({
                "criterion": "no_hallucination_on_invalid",
                "description": "无效输入下不凭空生成虚假的正常结果",
                "method": "pattern_check",
                "weight": 0.40,
                "scoring": {
                    "full_score": "未生成任何虚假的成功结果",
                    "zero_score": "在无效输入上生成了看似正确的虚假输出",
                },
            })
            return criteria

        # ── P1 minimal input: evaluate graceful handling, NOT completeness ────
        if source == "edge_minimal_input":
            criteria.append({
                "criterion": "graceful_handling",
                "description": "对最少输入的处理方式：主动询问缺失信息 vs 崩溃/静默",
                "method": "assess_recovery_behavior",
                "weight": 0.55,
                "scoring": {
                    "full_score": "主动询问缺少的信息，或生成带说明的基础输出",
                    "partial_score": "有部分输出，但缺乏引导",
                    "zero_score": "崩溃、静默失败，或生成与输入完全不符的幻觉内容",
                },
            })
            criteria.append({
                "criterion": "response_clarity",
                "description": "响应清晰度：不论是输出还是询问，用户都能理解下一步怎么做",
                "method": "holistic_assessment",
                "weight": 0.45,
                "scoring": {
                    "full_score": "用户能明确理解下一步操作",
                    "partial_score": "用户基本能理解但需要猜测",
                    "zero_score": "用户完全不知道发生了什么",
                },
            })
            return criteria

        # ── P1 optional feature skip: evaluate bypass behavior ───────────────
        if source == "optional_feature_skip":
            criteria.append({
                "criterion": "conditional_branch_correctness",
                "description": "可选功能跳过行为：未提供时正确跳过且不报错",
                "method": "verify_skip_behavior",
                "weight": 0.50,
                "scoring": {
                    "full_score": "正确跳过可选阶段，无报错，基础功能正常完成",
                    "partial_score": "跳过了阶段但有不必要的警告",
                    "zero_score": "因缺少可选参数而报错或崩溃",
                },
            })
            criteria.append({
                "criterion": "basic_output_quality",
                "description": "基础输出质量：跳过可选功能后仍能产出合理的基础结果",
                "method": "holistic_assessment",
                "weight": 0.50,
                "scoring": {
                    "full_score": "基础输出完整、合理，不受可选功能缺失影响",
                    "partial_score": "输出存在但内容缩减明显",
                    "zero_score": "无法产出任何基础输出",
                },
            })
            return criteria

        # ── P0/P1 happy-path cases: evaluate output quality ──────────────────
        remaining_weight = 1.0
        sample_sections = output_promises[:6] if output_promises else []

        # Output completeness (only for cases expecting structured output)
        if "structure" in key_checks or "completion" in key_checks:
            w = 0.40
            remaining_weight -= w
            criteria.append({
                "criterion": "output_completeness",
                "description": "必需内容/章节的完整程度",
                "method": "check_required_sections",
                "target_sections": sample_sections,
                "weight": w,
                "scoring": {
                    "full_score": "所有必需章节/内容均存在 (100%)",
                    "partial_score": "50%-99% 必需章节存在",
                    "zero_score": "少于 50% 必需章节",
                },
            })

        # Format compliance
        if "format" in key_checks or "structure" in key_checks:
            w = 0.20
            remaining_weight -= w
            criteria.append({
                "criterion": "format_compliance",
                "description": "输出格式是否符合文档规范（Markdown结构等）",
                "method": "validate_format",
                "format_type": "markdown",
                "weight": w,
                "scoring": {
                    "full_score": "格式完全合规",
                    "partial_score": "格式有小错误但可读",
                    "zero_score": "格式严重错误",
                },
            })

        # No hallucination (when relevant)
        if "no_hallucination" in key_checks:
            w = min(0.15, remaining_weight - 0.2)
            if w > 0:
                remaining_weight -= w
                criteria.append({
                    "criterion": "no_hallucination",
                    "description": "无幻觉内容（不凭空捏造数据、路径、参数）",
                    "method": "pattern_check",
                    "weight": w,
                    "scoring": {
                        "full_score": "无占位符/幻觉内容",
                        "zero_score": "存在明显幻觉或占位符内容",
                    },
                })

        # Fill remainder with overall quality
        if remaining_weight > 0.05:
            criteria.append({
                "criterion": "overall_quality",
                "description": "内容整体质量、相关性与专业程度",
                "method": "holistic_assessment",
                "weight": round(remaining_weight, 2),
                "scoring": {
                    "full_score": "专业、完整、与输入高度相关",
                    "partial_score": "基本符合预期但有改进空间",
                    "zero_score": "内容粗糙、不相关或存在重大错误",
                },
            })

        # Normalize weights
        total_w = sum(c["weight"] for c in criteria)
        if total_w > 0 and abs(total_w - 1.0) > 0.01:
            for c in criteria:
                c["weight"] = round(c["weight"] / total_w, 3)

        return criteria

    def _estimate_token_budget(
        self, priority: str, output_promises: List[str]
    ) -> Dict:
        """Estimate token budget based on skill complexity."""
        complexity = len(output_promises)
        if priority == "P0":
            expected_in = 2000 + complexity * 200
            expected_out = 3000 + complexity * 400
        elif priority == "P1":
            expected_in = 1000 + complexity * 100
            expected_out = 1500 + complexity * 200
        else:  # P2 error cases — short
            expected_in = 500
            expected_out = 500

        return {
            "expected_input_tokens": expected_in,
            "expected_output_tokens": expected_out,
            "max_acceptable_total": (expected_in + expected_out) * 3,
            "note": "Estimated. Actual measured during Layer 4.",
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self, data: Dict) -> None:
        self.evals_dir.mkdir(parents=True, exist_ok=True)
        with open(self.evals_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _archive(self, data: Dict) -> None:
        """Save a timestamped snapshot of the test suite to evals/archive/."""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = self.archive_dir / f"evals_{ts}.json"
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Archived test suite → {archive_path.name}")

    def _list_archives(self) -> List[Path]:
        """Return archived test suite files sorted by date (newest first)."""
        if not self.archive_dir.exists():
            return []
        return sorted(self.archive_dir.glob("evals_*.json"), reverse=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python layer3_generate_test_cases.py <skill-path> [--force] [--list-archives]")
        sys.exit(1)

    skill_path = sys.argv[1]
    force = "--force" in sys.argv
    list_archives = "--list-archives" in sys.argv

    generator = TestCaseGenerator(skill_path)

    if list_archives:
        archives = generator._list_archives()  # pylint: disable=protected-access
        if archives:
            print(f"\nArchived test suites ({len(archives)} total):")
            for a in archives:
                mtime = datetime.fromtimestamp(a.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                size = a.stat().st_size
                print(f"  {a.name}  ({size} bytes, {mtime})")
        else:
            print("No archives found.")
        return

    print(f"Generating test cases for: {skill_path}")
    print("=" * 70)

    result = generator.generate(force=force)

    if "error" in result:
        print(f"\n❌ Error: {result['error']}")
        sys.exit(1)

    print("\n✅ Test Case Generation Complete")
    print("\nCoverage:")
    cov = result["coverage"]
    print(f"  Happy path (P0): {cov['happy_path']}")
    print(f"  Edge cases (P1): {cov['edge_cases']}")
    print(f"  Error cases (P2): {cov['error_cases']}")
    print(f"  Total:           {cov['total']}")

    if result.get("mcp_tools_required"):
        print(f"\n🔌 MCP tools required: {result['mcp_tools_required']}")

    print("\n📝 Test Cases:")
    for tc in result["test_cases"]:
        n_criteria = len(tc.get("evaluation_criteria", []))
        print(f"  [{tc['priority']}] {tc['id']}: {tc['prompt'][:55]}... ({n_criteria} criteria)")

    print(f"\n📦 Archive: {generator.archive_dir}/")
    print("\n💡 Next Steps:")
    print("1. Review: {generator.evals_path}")
    print("2. Install required MCP tools if any")
    print("3. Run dynamic evaluation (Layer 4)")


if __name__ == "__main__":
    main()
