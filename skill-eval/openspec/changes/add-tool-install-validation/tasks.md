# Tasks: add-tool-install-validation

## Phase 1: 核心校验组件

### Task 1.1: 新增 ToolAvailabilityChecker
- **文件**: `evaluator/layers/tool_checker.py` (新建)
- **内容**: 实现 `ToolAvailabilityChecker`、`ToolStatus`、`ToolAvailabilityResult`
- **验证**: 单元测试 `tests/test_tool_checker.py`

### Task 1.2: 扩展 LayerResult 模型
- **文件**: `evaluator/models/results.py`
- **内容**: 增加 `tool_availability: dict[str, bool] | None` 可选字段
- **验证**: 模型序列化/反序列化测试

### Task 1.3: 集成到 Layer2StaticAnalysis
- **文件**: `evaluator/layers/layer2_static.py`
- **内容**:
  - 在 `run()` 开头调用 `ToolAvailabilityChecker.check_all()`
  - 不可用工具跳过时标记 `status="SKIP"` 而非 `status="PASS"`
  - 全部不可用时返回 `BLOCKED` 状态
- **验证**: 现有 Layer 2 测试用例通过

## Phase 2: 配置与报告

### Task 2.1: 增加配置项
- **文件**: `evaluator/config.py`
- **内容**: `tool_availability_check` (bool) 和 `tool_missing_policy` (str)
- **验证**: 配置加载测试

### Task 2.2: HTML 报告展示工具可用性
- **文件**: `evaluator/reporters/`
- **内容**: 在 Layer 2 区域增加工具可用性指示器
- **验证**: 报告生成后检查 HTML 内容

## Phase 3: 降级逻辑

### Task 3.1: DEGRADED 模式分数调整
- **文件**: `evaluator/layers/layer2_static.py`
- **内容**: 部分工具不可用时，按可用工具比例缩减维度权重
- **验证**: 带 mock 的测试用例

### Task 3.2: BLOCKED 模式处理
- **文件**: `evaluator/layers/layer2_static.py`, `evaluator/pipeline.py`
- **内容**: 全部工具不可用时 Layer 2 得分为 0，结果中标记 BLOCKED
- **验证**: 集成测试