# Spec: Tool Availability Checker

## Overview

`ToolAvailabilityChecker` 负责在 Layer 2 静态分析启动前，校验所有内置工具是否已安装且可运行。

## API

```python
class ToolAvailabilityChecker:
    """校验 Layer 2 所需工具的安装和运行状态。"""

    TOOLS: ClassVar[list[str]] = ["pylint", "radon", "bandit", "pip-audit"]

    async def check_all(self) -> ToolAvailabilityResult:
        """并发检查所有工具，返回聚合结果。"""
        ...

    async def check_single(self, tool_name: str) -> ToolStatus:
        """检查单个工具：先 which 确认路径，再 --version 确认可运行。"""
        ...
```

## Data Models

```python
@dataclass
class ToolStatus:
    tool_name: str          # pylint / radon / bandit / pip-audit
    available: bool         # 是否已安装且可运行
    path: str | None        # 可执行文件路径（shutil.which 结果）
    version: str | None     # --version 输出
    error: str | None       # 失败原因

@dataclass
class ToolAvailabilityResult:
    tools: dict[str, ToolStatus]   # key=tool_name
    all_available: bool            # 全部可用
    available_count: int
    missing_count: int
    summary: str                   # 人可读的摘要
```

## Behavior

### 正常流程
1. 对每个工具执行 `shutil.which(tool_name)` → 确认可执行文件存在
2. 执行 `subprocess.run([tool_name, "--version"], capture_output=True, timeout=10)` → 确认可运行
3. 任一失败即标记 `available=False`

### 降级策略
- `all_available=True` → Layer 2 正常评测
- `0 < available_count < 4` → Layer 2 进入 DEGRADED 模式，不可用工具跳过，dimension score 按比例缩减
- `available_count == 0` → Layer 2 标记为 BLOCKED，score=0

### 错误处理
- `subprocess.TimeoutExpired` → `available=False, error="timeout"`
- `FileNotFoundError` → `available=False, error="not found"`
- 非零退出码 → `available=False, error="exit code: {rc}"`

## Config Integration

在 `config.py` 中增加：

```python
class EvalConfig:
    # 是否启用工具可用性预校验（默认 True）
    tool_availability_check: bool = True
    # 工具不可用时 Layer 2 行为: "degrade" | "block" | "warn"
    tool_missing_policy: str = "degrade"
```

## Report Integration

HTML 报告中在 Layer 2 区域增加工具可用性指示器：
- 🟢 全部可用：绿色 "All tools available"
- 🟡 部分可用：黄色 "N/M tools available"
- 🔴 全部不可用：红色 "No tools available - Layer 2 BLOCKED"