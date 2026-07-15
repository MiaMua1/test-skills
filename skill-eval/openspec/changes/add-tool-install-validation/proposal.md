# Proposal: add-tool-install-validation

## Summary

为 `skill-evaluator` 的 Layer 2（静态分析层）增加内置工具（pylint、radon、bandit、pip-audit）安装与运行状态的预校验机制。当前当工具未安装或运行失败时，评测层静默返回 `None` 并以 "工具不可用" 放行，导致评测结果失真——工具缺失的 Skill 可能获得不应有的高分。

## Motivation

### 现状问题

在 `evaluator/layers/layer2_static.py` 中，四个内置静态分析工具（pylint、radon、bandit、pip-audit）的执行方法均采用 `try/except` 捕获异常并在失败时返回 `None`。上层调用方对 `None` 的处理逻辑为：

```python
# pylint 示例 (line ~178-181)
if pylint_result is None:
    self.log.warning("pylint 工具不可用，跳过代码质量检查")
    quality_checks.append({"name": "pylint", "status": "PASS", "detail": "工具不可用", "weight": "25%"})
```

四个工具均采用相同模式：工具不可用时 `status="PASS"`，不产生任何扣分。这导致：

1. **评测结果失真**：工具缺失环境下评测的 Skill 获得虚高分数
2. **问题不可见**：用户无法区分"代码质量优秀"与"工具未安装"
3. **无阻断机制**：即使所有工具都不可用，评测仍正常完成，总评分无任何惩罚

### 期望行为

1. **启动前校验**：在 Layer 2 执行前，检查所有所需工具是否已安装且可运行
2. **分级处理**：
   - 工具全部可用 → 正常评测
   - 部分工具不可用 → 继续评测但降低该维度权重或标记为 `DEGRADED`
   - 全部工具不可用 → 该层标记为 `BLOCKED`，总评分中体现
3. **结果透明**：在评测报告中明确标注哪些工具不可用及其影响

## Scope

### In Scope
- 新增 `ToolAvailabilityChecker` 组件，在 Layer 2 启动前校验 pylint/radon/bandit/pip-audit 的可用性
- 修改 `Layer2StaticAnalysis` 的 `run()` 方法，集成校验结果
- 修改 `LayerResult` 模型，增加 `tool_availability` 字段
- 在 HTML 报告中展示工具可用性状态

### Out of Scope
- 自动安装缺失工具（保持环境纯净性）
- Layer 4 动态执行层的工具校验（该层主要依赖 LLM API，非静态工具）
- 工具版本兼容性检查（仅检查安装/运行，不检查版本）

## Affected Components

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `evaluator/layers/layer2_static.py` | 修改 | 集成 ToolAvailabilityChecker，修改工具不可用时的处理逻辑 |
| `evaluator/models/results.py` | 修改 | 增加 `tool_availability` 字段 |
| `evaluator/layers/base.py` | 可能修改 | 若需要抽象通用校验接口 |
| `evaluator/reporters/` | 修改 | HTML 报告增加工具可用性展示 |
| `evaluator/config.py` | 可能修改 | 增加工具可用性相关的配置开关 |
| `evaluator/layers/__init__.py` | 可能修改 | 导出新组件 |

## Risks

- **向后兼容**：`LayerResult` 模型变更可能影响已有评测结果的解析，需确保 `tool_availability` 字段为可选（`Optional`）
- **环境差异**：不同环境下工具安装路径不同，需使用 `shutil.which()` 或 `subprocess` 版本检查而非硬编码路径
- **性能影响**：启动前校验增加约 1-2 秒开销（四次 `--version` 调用），可接受

## Alternatives Considered

1. **在 `_run_xxx` 方法内部修改返回值**：改动最小，但无法区分"工具未安装"和"工具运行异常"，且缺乏统一的校验入口
2. **使用 Docker 统一环境**：彻底解决环境差异，但引入 Docker 依赖，增加使用门槛
3. **当前方案（ToolAvailabilityChecker）**：统一校验入口，结果可复用，改动清晰可控

## Success Criteria

- [ ] 所有四个工具（pylint/radon/bandit/pip-audit）安装且可运行时，Layer 2 正常评测，行为与现状一致
- [ ] 任一工具不可用时，Layer 2 结果中明确标注 `tool_availability` 状态
- [ ] 全部工具不可用时，Layer 2 得分为 0 或标记为 BLOCKED
- [ ] HTML 报告中可视化展示工具可用性
- [ ] 现有测试用例通过