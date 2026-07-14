---
name: skill-evaluator
version: "1.0.0"
type: analyzer
description: "通用 AI Skill 综合质量评测框架。可评测任何 AI Skill / Agent / 工具能力，不限于 Cursor Skill。当用户需要评测 Skill 质量、检查合规性、运行质量评估、给 Skill 打分、测量增量价值、验证安全性，或提供任意 skill 路径请求质量检测时使用。触发关键词：「评测这个 skill」「检查 skill 质量」「给这个 skill 打分」「skill 有多少增量价值」「评估 skill」「运行评测」「安全检查」「批量评测」，或用户提供了 skill/agent/工具 路径要求质量检测时。凡涉及 Skill 评测，优先使用此框架。"
author: skill-evaluator-team
---

# 通用 Skill 质量评测框架

> 文档最后更新：2026-03-30

## Description

面向所有 AI Skill / Agent / 工具能力的综合质量评测系统，实现多层次自动化测试、静态代码扫描与基线对比，输出 0-100 分评分和 A-F 等级，并生成 HTML 可视化报告。框架本身封装为单个 Skill tool，Claude 调用后同步等待完整评测结果。

**核心评测逻辑**：
```
With-Skill 测试（始终执行）：
  prompt → Claude + Skill tool → 执行 → 输出
                                           ↓
                                    LLM Judge 打分（依据 scoring_criteria）
                                           ↓
                                    基础得分（满分 100）

Without-Skill 基线（可选，通过 --with-baseline 启用）：
  prompt → Claude（无tool）→ 输出
                                  ↓
                           LLM Judge 打分（相同标准）
                                  ↓
                           增值 Bonus（最高 +30，独立于基础分）
```

---

## 触发条件

以下情况触发本 skill：
- 用户说「评测这个 skill」「检查 skill 质量」「给这个 skill 打分」
- 用户提供 skill 路径并要求质量评估
- 用户需要验证 skill 的安全性或合规性
- 用户询问 skill 评分、通过率或基准测试结果
- 用户提及「skill 评测」「质量检查」「评分」「安全扫描」
- 用户需要对比 skill 版本或衡量改进效果

---

## 五层 + 聚合评测架构

```
第一层  快速筛查     元数据+文档完整性                    <30秒
        阻塞：score < layer1_max × 0.75

第二层  静态分析     代码质量+安全合规                    <30秒
        无代码文件自动跳过，记满分
        阻塞：发现 CRITICAL 漏洞

第三层  用例生成     生成测试用例 + 动态评分标准           1-2分钟
        产物1：storage/evals/{skill_name}/{eval_id}/evals.json
        产物2：storage/evals/{skill_name}/{eval_id}/scoring_criteria.json

第四层  批跑评估     执行用例，完整保存输入输出快照        2-10分钟
        每条用例：storage/results/{skill_name}/{eval_id}/with_skill/{tc_id}.json
                  storage/results/{skill_name}/{eval_id}/without_skill/{tc_id}.json（仅 --with-baseline 时）
        阻塞：P0用例健壮性全部失败

第五层  报告生成     从存档读取数据，强绑定 eval_id        <10秒
        产物1：storage/reports/{skill_name}/{eval_id}/eval_data.json
        产物2：storage/reports/{skill_name}/{eval_id}/report.html
        报告包含独立的「增量价值分析」section（需启用 --with-baseline）

聚合层  聚合报告     同类 skill 横向对比                  <30秒
        触发条件：同 eval_profile + 权重快照一致 + ≥2个评测
        产物1：storage/aggregate/{profile_type}/{aggregate_id}/aggregate_data.json
        产物2：storage/aggregate/{profile_type}/{aggregate_id}/aggregate_report.html
```

---

## Parameters

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `skill_path` | string | 是 | — | 待评测 skill 的目录路径（含 SKILL.md 的目录），或 GitHub URL |
| `mode` | string | 否 | `full` | `full`（全部层）/ `quick`（L1+L2）/ `custom` |
| `env` | string | 否 | `auto` | `auto`（优先 Docker）/ `docker` / `local` |
| `output` | string | 否 | `both` | `html` / `markdown` / `both` |
| `aggregate` | bool | 否 | `false` | 是否触发聚合报告 |
| `eval_ids` | string | 否 | — | 聚合时指定多个 eval_id（逗号分隔） |
| `evals_file` | string | 否 | — | 本地用例文件路径（evals.json 或 JSON 数组）。提供后跳过 L3 自动生成，直接使用文件中的测试用例 |
| `criteria_file` | string | 否 | — | 本地评分标准文件路径（scoring_criteria.json 或 JSON 数组）。提供后使用文件中的评分规则；`profile_weight_snapshot` 始终从当前配置重新写入，文件中的值不生效 |
| `judge_model` | string | 否 | `JUDGE_MODEL` 环境变量 | 覆盖 L4 评分 Judge 使用的 LLM 模型（如 `claude-opus-4-5`、`gpt-4o`、`glm-4`）。需要 API Key 才能生效，否则降级为 rule_based。仅对本次评测生效 |
| `eval_model` | string | 否 | `EVAL_MODEL` 环境变量 | 覆盖 L4 LLM API 路径执行测试用例时使用的模型。**Claude CLI 路径不受此控制**（CLI 使用本地默认模型）。仅对本次评测生效 |
| `with_baseline` | bool | 否 | `false` | 启用 without-skill 基线对比（增值模块）。开启后 deterministic/workflow 额外获得最高 30 分增值 Bonus（独立于基础 100 分，不影响正确性权重）；关闭时 delta_max=0，基础满分 100。评测完成后会提示用户可启用此选项 |

---

## Profile 感知评分权重

根据 skill 类型，分值在各层的分配不同：

| Profile | 基础合规 (L1) | 代码质量 (L2) | 安全合规 (L2) | 健壮性 (L4) | 正确性 (L4) | 基础合计 | 增值 Bonus (L4) |
|---------|------|------|------|------|------|-------|-------|
| `deterministic`（tool/analyzer） | 15 | 15 | 20 | 8 | 42 | **100** | +30 |
| `generative`（generator） | 15 | 5 | 15 | 10 | 55 | **100** | — |
| `workflow` | 15 | 10 | 15 | 8 | 52 | **100** | +30 |
| `no_code` | 20 | 0 | 10 | 15 | 55 | **100** | — |

> **默认配置（without-skill 基线对比关闭）**：所有 profile 的基础满分 = 100，不含增值模块。
> **启用基线对比（--with-baseline）**：deterministic/workflow 额外获得最高 30 分增值 Bonus（独立于基础 100 分，不影响正确性权重）。评测完成后会提示用户可启用此选项。
> **generative / no_code**：增值模块不适用，跳过 without-skill 运行。

**Profile 推断链（按优先级）**：
1. `skill.json` 或 SKILL.md frontmatter 有明确 `type` → 直接使用
2. 无代码文件 → `no_code`
3. 有代码 + 描述含 workflow/pipeline/orchestrat/工作流/流程/编排/多步骤/链式/阶段 → `workflow`
4. 有代码 + 描述含 generat/creat/write/produc/draft/生成/创作/撰写/产出/起草/输出文档 → `generative`
5. 有代码 + 描述含 analyz/extract/classif/detect/parse/evaluat/分析/提取/分类/检测/解析/评估/审查 → `deterministic`（analyzer）
6. 有代码，无关键词匹配 → `deterministic`（tool）

推断结果记录 `type_inferred = True`，第一层扣合规分：`type 缺失 penalty = layer1_max × 0.05`

**分数计算公式**：
```
layer1_score  = 各检查项得分之和

layer2_score  = quality_score + security_score
  quality_score  = quality_raw(0-1)  × quality_max
  security_score = security_raw(0-1) × security_max

layer4_base   = robust_score + correct_score
  robust_score  = avg_robust(0-1)  × robust_max
  correct_score = avg_correct(0-1) × correct_max

base_score = layer1_score + layer2_score + layer4_base    # 满分 100

# 增值 Bonus（独立模块，仅在 --with-baseline 启用时计算）
# deterministic/workflow 最高 +30 分；generator/no_code 不适用
delta_raw        = with_correct(0-1) - without_correct(0-1)   # -1.0 ~ +1.0
delta_normalized = max(0, delta_raw + 0.5)                     # 0.0  ~ 1.0
delta_score      = delta_normalized × delta_max                # 0 ~ 30
（默认配置 / generator / no_code：delta_score = 0）

total_score = base_score + delta_score                         # 满分 100（或 100+30）
```

**等级判定**：
```
A ≥ 90 → PASSED
B ≥ 75 → PASSED
C ≥ 60 → PASSED
D ≥ 45 → NEEDS_IMPROVEMENT
F < 45 → FAILED
```

---

## 存储结构

```
storage/
├── evals/
│   └── {skill_name}/
│       └── {eval_id}/
│           ├── evals.json                  # 测试用例集
│           └── scoring_criteria.json       # 动态评分标准（与 evals.json 同步生成）
│
├── results/
│   └── {skill_name}/
│       └── {eval_id}/
│           ├── with_skill/
│           │   └── {tc_id}.json            # 完整输入输出快照
│           └── without_skill/
│               └── {tc_id}.json            # 完整输入输出快照
│
├── reports/
│   └── {skill_name}/
│       └── {eval_id}/
│           ├── eval_data.json              # 结构化评测数据
│           └── report.html                 # HTML 报告
│
└── aggregate/
    └── {profile_type}/
        └── {aggregate_id}/
            ├── aggregate_data.json         # 聚合数据
            └── aggregate_report.html       # 聚合 HTML 报告

eval_id 格式：{skill_name}-{YYYYMMDD-HHMMSS}-{uuid4前8位}
aggregate_id 格式：{profile_type}-aggregate-{YYYYMMDD-HHMMSS}-{uuid4前8位}
```

> storage/ 目录已加入 .gitignore，不提交到版本控制。

---

## 评测约定（Runtime Conventions）

以下约定在所有评测场景中强制生效，**不随评测模式或 skill 类型变化**。

### 约定1 — 只读评测

评测过程只读被评测 skill 的文件，**严禁写入或修改**被评测 skill 目录下任何内容。发现问题按原始状态记录并返回评测结果，不做自动修复。

### 约定2 — 干净子环境 + 日期注入

子 agent（Claude CLI 子进程 / judge_worker 子进程）启动时必须：

1. **剥离**父环境中 `CLAUDECODE`、`CURSOR_AGENT`、`CURSOR_EXTENSION_HOST_ROLE` 三个 env var，防止嵌套 agent 检测误触发
2. **注入当天日期**：通过 `--append-system-prompt` 或 prompt context 注入 `今天是 YYYY-MM-DD`，确保子进程中 LLM 具有时间感知

### 约定3 — token 隔离 + 可观测

- **LLM judge 调用**走 `judge_worker.py` 子进程（`asyncio.create_subprocess_exec`），token 消耗记账在子进程，不消耗父 agent token 预算
- **子 agent 调用可见**：父 agent 通过 structlog 日志可感知所有子 agent 调用：
  - `layer4.cli_launch` — Claude CLI 子进程启动
  - `layer4.cli_done` — CLI 完成（含 duration_s、exit_code）
  - `layer4.judge_worker_launch` — judge worker 子进程启动（在 `_batch_judge_via_worker` 中记录）
  - `layer4.batch_judge_success` — judge 完成（含 tokens）

### 约定4 — 超时隔离 + 熔断器

L4 执行路径按优先级自动选择（MCP bridge 已移除）：

| 优先级 | 路径 | 使用的模型 | 条件 |
|--------|------|-----------|------|
| 1 | Claude CLI | CLI 本地默认模型 | 系统已安装 `claude` CLI |
| 2 | Anthropic API | `EVAL_MODEL` | 有 `ANTHROPIC_API_KEY` |
| 3 | OpenAI 兼容 API | `EVAL_MODEL` | 有 `OPENAI_API_KEY` |
| 4 | 本地入口点 | — | 直接运行 `skill_entry.py` |

Claude CLI 子进程采用三层保护，**任一超时不阻塞父 agent**：

| 保护层 | 参数 | 说明 |
|--------|------|------|
| CLI 预检 probe | 15s | 首次 TC 前发 trivial prompt；失败则立即禁用 CLI |
| CLI 内部 claude 超时 | 80s | `claude -p` 的 wall-clock 限制 |
| CLI 外层 asyncio 超时 | 90s | `asyncio.wait_for` + `proc.kill()` 真正杀死进程 |
| CLI 熔断器 | 连续 2 次 | timeout/失败 2 次 → `_cli_disabled=True` → 后续 TC 走 LLM API |
| API 认证熔断器 | 连续 3 次 | API 认证失败 3 次 → `_api_auth_blocked=True` → 阻断评测 |

评判（Scoring）降级机制（独立于执行路径）：

| 优先级 | 方式 | 条件 |
|--------|------|------|
| 1 | 程序化检查 | `deterministic`/`workflow` profile，置信度 ≥ 0.7 |
| 2 | LLM Judge | 有 API Key，通过 `judge_worker.py` 子进程调用 `JUDGE_MODEL` |
| 3 | 规则匹配 fallback | 无 API Key → 关键词匹配（`eval_method: "rule_based"`） |

Judge worker（scoring）采用独立超时：

| 保护层 | 参数 |
|--------|------|
| `asyncio.wait_for` + `proc.kill()` | 60s |

### 约定5 — 过程日志

L4 动态评测期间实时写入进度文件，供父进程轮询：

```
storage/progress/{skill_name}/{eval_id}.json
```

字段：`phase`（initializing / running / completed / error）、`total`、`completed`、`current`（当前 TC id）、`status`、`updated_at`

排查问题时读取此文件可快速定位当前执行位置。

### 约定6 — 自动确认

动态评测中，若被测模型在 with_skill / without_skill 执行时**中途反问或请求人工确认**（如"请先执行脚本"、"请告诉我结果"），评测框架通过 `_is_response_complete()` 检测此类不完整响应，自动标记 `incomplete_response=True` 并将该用例 `not_empty` 健壮性检查置为失败，**评测继续推进，不等待人工输入**。

---

## 环境准备（首次使用必读）

在执行任何评测步骤之前，**必须先确保 evaluator 依赖已安装**。

### 检查是否已安装

```bash
cd <skill-evaluator 目录>
python3 -c "import evaluator; print('OK')"
```

输出 `OK` 则跳过安装；否则按下方步骤操作。

### 安装方式（按顺序尝试）

**方式 1 — 推荐：使用虚拟环境（所有系统均适用）**

```bash
cd <skill-evaluator 目录>
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

> ⚠️ 激活 venv 后，后续所有 `python3` / `python3 -m evaluator.cli` 命令都在这个环境里执行。

**方式 2 — 回退：--user 安装**

```bash
pip install --user -r requirements.txt
```

**方式 3 — macOS Python 3.12 / 3.13 报 "externally-managed" 或权限错误时**

```bash
# 报错形如：OSError: [Errno 1] Operation not permitted: '.../Python/3.13'
# 或：error: externally-managed-environment
pip install --break-system-packages -r requirements.txt
```

> 如果以上都失败，强烈建议通过 `pyenv` 或 Homebrew 安装一个用户可控的 Python（≥3.11），再用方式 1 创建 venv。

### 验证安装

```bash
bash
python3 -m evaluator.cli --help
```

输出帮助信息即安装成功。

### 环境变量配置（建议）

在项目根目录创建 `.env`（按需填写）：

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=
OPENAI_BASE_URL=

JUDGE_MODEL=claude-opus-4-5          # 评判模型（需要 API Key 才生效，否则降级为 rule_based）
EVAL_MODEL=claude-opus-4-5           # LLM API 路径执行模型（Claude CLI 路径不受此控制）

# 可选运行时参数
STORAGE_BASE_DIR=./storage
LAYER3_TIMEOUT=120
LAYER4_CASE_TIMEOUT=60
LAYER4_TOTAL_TIMEOUT=600
JUDGE_PASSING_THRESHOLD=0.7
DELTA_NORMALIZE_OFFSET=0.5
```

---

## 评测流程

### 步骤 0：解析用户请求

识别以下信息：
- **Skill 路径**：待评测 skill 的目录（包含 SKILL.md 的目录）或 GitHub URL
- **评测模式**：`full`（完整 5 层）、`quick`（L1+L2 静态）或 `custom`（指定层）
- **环境偏好**：`docker`、`local` 或 `auto`（优先 Docker，回退本地）
- **输出格式**：`html`、`markdown` 或 `both`

若未提供 skill 路径，询问用户。其他选项使用智能默认值。

**生成 eval_id**：
```python
import uuid
from datetime import datetime

skill_name = Path(skill_path).name  # kebab-case
ts = datetime.now().strftime("%Y%m%d-%H%M%S")
uid = str(uuid.uuid4())[:8]
eval_id = f"{skill_name}-{ts}-{uid}"
```

### 步骤 1：第一层 — 快速筛查（必须执行）

**目的**：快速阻断明显不合规的 skill。满分 = `layer1_max`（按 profile 取值）。

**实现**：调用 `evaluator/layers/layer1_screening.py`

**三类检查**：

1. **元数据（40% of layer1_max）**：
   - 有 `skill.json` 时（强制检查）：`name` 符合 kebab-case（`^[a-z0-9-]+$`）、`version` 符合 semver（`^\d+\.\d+\.\d+$`）、`type` 为合法值（`tool|analyzer|generator|workflow`）、`description` 至少 20 个字符、`author` 存在
   - 仅有 SKILL.md frontmatter 时（降级检查）：`name`、`description`，`version`/`type` 缺失时 penalty 减半

2. **文档完整性（40% of layer1_max）**：
   - SKILL.md 存在且可读
   - 包含描述性介绍（`## Description` 或等价内容）
   - 包含参数说明（`## Parameters` 表格/列表）
   - 包含使用示例（`## Examples`，含输入/输出对照）
   - 包含返回值说明（`## Returns` 或输出描述章节）

3. **基础合规（20% of layer1_max）**：
   - 无超过 10MB 的文件
   - 无可疑可执行文件（`.exe` / `.bat` / `.cmd` / `.vbs`）
   - 文件名无明显恶意特征

**阻塞规则**：`layer1_score < layer1_max × 0.75` → BLOCKED，停止评测，列出缺失项并给出修复建议。

### 步骤 2：第二层 — 静态分析（条件执行）

**目的**：代码质量 + 安全合规。`has_code = False` 时自动跳过，授予 `quality_max + security_max` 满分。

**实现**：调用 `evaluator/layers/layer2_static.py`

**代码文件扫描规则**：
- 递归扫描 `skill_path` 下所有子目录
- 排除目录：`node_modules/`, `.venv/`, `venv/`, `env/`, `__pycache__/`, `.git/`, `dist/`, `build/`, `*.egg-info/`
- 代码后缀：`.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.sh`, `.rb`, `.go`
- 存在任意代码文件 → `has_code = True`

**A：代码质量**（最高 `quality_max` 分，no_code 跳过）：
- 优先运行真实工具：`pylint`（按错误/警告比例扣分）、`radon cc`（CC > 10 扣分）、类型注解覆盖率
- 无法执行工具时，LLM 推理评估（报告注明 `evaluation_method: "LLM推理"`）

**B：安全合规**（最高 `security_max` 分）：
- 优先运行真实工具：`bandit -r`（CRITICAL → **BLOCKED**；HIGH → 扣最多 security_max × 50%）、`pip-audit`（CRITICAL CVE → 扣最多 security_max × 30%）
- 无法执行工具时，LLM 推理审查（检查命令注入、硬编码密钥、eval/exec、路径遍历等）

**阻塞规则**：发现任何 CRITICAL 漏洞 → BLOCKED。

### 步骤 3：第三层 — 测试用例 + 评分标准生成（1-2 分钟）

**目的**：同步生成 `evals.json` 和 `scoring_criteria.json`，共享 `eval_id`。

**实现**：调用 `evaluator/layers/layer3_testgen.py`

**生成流程**：
```
1. 检查 storage/evals/{skill_name}/ 是否已有对应用例，有则复用
2. 解析 SKILL.md，提取示例、参数、预期输出、边界情况
3. 按优先级生成：P0（3个，核心功能）/ P1（4个，边界用例）/ P2（3个，错误处理）
4. 每个用例生成 robustness_checks、correctness_rubric、baseline_prompt
5. 保存 evals.json 到 storage/evals/{skill_name}/{eval_id}/evals.json
6. 立即生成 scoring_criteria.json 到同目录，不可分批
```

**健壮性检查类型（按 has_code 选择）**：

有代码 skill：
```
no_exception    → 捕获运行时异常
not_empty       → 检查输出非空
timeout         → 检查执行时间
exit_code       → 检查进程退出码
contains_field  → 检查输出字段
```

无代码 skill：
```
doc_coverage    → SKILL.md 是否覆盖该用例场景
param_valid     → 参数说明是否完整
example_match   → Examples 是否包含类似输入
logic_coherent  → 描述逻辑是否自洽
```

**evals.json 结构**：
```json
{
  "skill_name": "web-search",
  "eval_id": "web-search-20240312-143022-a1b2c3d4",
  "skill_type": "tool",
  "eval_profile": "deterministic",
  "generated_at": "ISO时间戳",
  "coverage": {
    "p0_count": 3,
    "p1_count": 4,
    "p2_count": 3
  },
  "test_cases": [
    {
      "id": "tc_001",
      "priority": "P0",
      "source": "auto",
      "prompt": "用户实际会输入的自然语言任务",
      "expected_behavior": "期望发生什么",
      "context": {},
      "robustness_checks": [
        {"description": "执行无异常抛出", "check_type": "no_exception"},
        {"description": "返回结果非空", "check_type": "not_empty"}
      ],
      "correctness_rubric": [
        {"criterion": "返回结果包含必要字段", "weight": 2.0},
        {"criterion": "格式符合预期规范", "weight": 1.0}
      ],
      "baseline_prompt": "不假设有 skill 可用的同等提问（generator/no_code 为 null）"
    }
  ]
}
```

**scoring_criteria.json 结构**：
```json
{
  "eval_id": "web-search-20240312-143022-a1b2c3d4",
  "skill_name": "web-search",
  "eval_profile": "deterministic",
  "generated_at": "ISO时间戳",
  "profile_weight_snapshot": {
    "layer1_max": 15,
    "quality_max": 15,
    "security_max": 20,
    "robust_max": 8,
    "correct_max": 12,
    "delta_max": 30
  },
  "criteria_by_tc": [
    {
      "tc_id": "tc_001",
      "weight_snapshot": {"robust_max": 8, "correct_max": 12, "delta_max": 30},
      "robustness_scoring": [
        {
          "check_id": "r_001",
          "description": "执行无异常抛出",
          "check_type": "no_exception",
          "pass_score": 1.0,
          "fail_score": 0.0,
          "weight": 1.5
        }
      ],
      "correctness_scoring": [
        {
          "assertion_id": "c_001",
          "criterion": "返回结果包含必要字段",
          "weight": 2.0,
          "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0},
          "scoring_guidance": "由 LLM 针对本用例动态生成的评分指引原文"
        }
      ],
      "delta_scoring": {
        "delta_max": 30,
        "formula": "max(0, with_correct - without_correct + 0.5) × 30",
        "guidance": "对比有无 skill 时的正确性得分差值，持平得 50% delta 分"
      }
    }
  ]
}
```

**第三层关键约束**：
- `profile_weight_snapshot` 从当前 ScoreProfile 实时读取并冻结，禁止手填
- `correctness_scoring` 的 `scoring_guidance` 由 LLM 针对每个用例动态生成
- `generator / no_code` 类：`delta_scoring` 字段为 null
- `no_code` 类：`robustness_scoring` 使用 doc_coverage 等文档检查类型
- 两个文件写入同一目录，`eval_id` 必须完全一致
- `evals.json` 写入完成后立即生成 `scoring_criteria.json`，不可分批

### 步骤 4：第四层 — 批跑评估（2-10 分钟）

**目的**：执行全部测试用例，完整保存每条用例的输入输出快照，基于 `scoring_criteria.json` 驱动评分。

**实现**：调用 `evaluator/layers/layer4_dynamic.py`

**执行流程**：
```
1. 读取 evals.json + scoring_criteria.json，校验 eval_id 一致
2. Bridge 预检 probe（约定4）：发 trivial prompt 15s timeout
   - probe 失败 → bridge 禁用，后续所有 TC 走 LLM API
   - probe 成功 → 允许使用 bridge
3. 按优先级执行调用链（每条 TC 独立）：
   claude CLI（独立进程）→ MCP bridge（熔断 2 次后跳过）→ LLM API with tools → 本地执行
4. 对每条用例执行两种模式（generator/no_code 跳过 without_skill；默认配置下 deterministic/workflow 也跳过，仅 --with-baseline 时启用）：
   - with_skill：使用 skill 执行 prompt
   - without_skill：不使用任何 skill 执行 baseline_prompt（仅 --with-baseline 时执行）
5. 每条用例执行完立即写入快照到 storage/results/，并更新 storage/progress/ 进度文件
6. 执行失败时记录异常信息，不中断批跑
7. 若被测模型中途反问或请求确认，_is_response_complete() 标记为不完整并继续（约定6）
8. 全部执行完毕后汇总三个子维度得分
```

**进度追踪**：执行期间可轮询 `storage/progress/{skill_name}/{eval_id}.json` 查看当前进度（详见约定5）。

**三个子维度**：
- **健壮性**：`robust_score = avg_robust(0-1) × robust_max`
- **正确性**：LLM Judge 依据 `scoring_guidance` 对输出评估，`correct_score = avg_correct(0-1) × correct_max`
- **增量价值**：`delta_raw = with_correct - without_correct`；`delta_normalized = max(0, delta_raw + 0.5)`；`delta_score = delta_normalized × delta_max`

**单条用例结果文件 `{tc_id}.json`**：
```json
{
  "tc_id": "tc_001",
  "eval_id": "web-search-20240312-143022-a1b2c3d4",
  "skill_name": "web-search",
  "run_mode": "with_skill",
  "executed_at": "ISO时间戳",
  "duration_seconds": 3.42,
  "status": "success",
  "input": {
    "prompt": "用户发送的完整 prompt 原文",
    "skill_used": "web-search",
    "skill_version": "1.0.0",
    "context": {}
  },
  "output": {
    "raw_response": "Claude 返回的原始文本完整内容",
    "tool_calls": [],
    "final_answer": "最终回答文本"
  },
  "robustness_results": [
    {"check_id": "r_001", "check_type": "no_exception", "passed": true, "detail": ""}
  ],
  "correctness_results": [
    {
      "assertion_id": "c_001",
      "criterion": "返回结果包含必要字段",
      "score": 1.0,
      "level": "完全满足",
      "reasoning": "LLM Judge 的评分理由原文"
    }
  ],
  "scores": {"robust_raw": 1.0, "correct_raw": 0.87}
}
```

**阻塞规则**：全部 P0 用例健壮性检查均失败 → BLOCKED at Layer 4。

### 步骤 5：第五层 — 报告生成（< 10 秒）

**目的**：从存档文件读取数据，强绑定 `eval_id`，生成 `eval_data.json` 和 `report.html`。

**实现**：调用 `evaluator/layers/layer5_report.py` + `evaluator/reporters/html_reporter.py`

**生成前三项校验（必须全部通过）**：
1. `eval_id` 一致性：`scoring_criteria / results / report` 三者必须一致，否则抛 `ScoreBindingError`
2. `profile_weight_snapshot` 各项之和 = 100，不等则抛 `ScoreBindingError`
3. `score_breakdown` 每项 `max_score` 从快照读取，禁止手填

**生成步骤**：
```
1. 读取 scoring_criteria.json，提取 eval_id 和 profile_weight_snapshot
2. 校验三项约束（见上）
3. 从各层执行结果读取实际得分
4. 组装 score_breakdown
5. 写入 storage/reports/{skill_name}/{eval_id}/eval_data.json
6. 生成 storage/reports/{skill_name}/{eval_id}/report.html（Jinja2 模板渲染）
7. 输出结构化结果给用户
```

**报告可观测性补充**：
- L4 “测试用例详情”默认按执行路径分层展示
- 若出现工具调用轮次耗尽（`max_iterations=8`），报告会新增独立失败专栏并按执行路径再次分层，便于排查

**报告输出路径**：必须输出到 `storage/reports/{skill_name}/{eval_id}/report.html`，严禁覆盖 `evaluator/reporters/templates/report.html.j2` 模板文件。

### 步骤 6（可选）：聚合层 — 聚合报告

**目的**：对同类 skill 的多次评测结果进行横向汇总对比。

**实现**：调用 `evaluator/layers/layer6_aggregate.py`

**触发条件**（同时满足）：
- `eval_profile` 相同
- `profile_weight_snapshot` 完全一致（逐字段比较）
- 提供 2 个及以上已完成评测的 `eval_id`
- 每个 `eval_id` 对应的 `eval_data.json` 和 `scoring_criteria.json` 均存在

**输出**：
- `storage/aggregate/{profile_type}/{aggregate_id}/aggregate_data.json`
- `storage/aggregate/{profile_type}/{aggregate_id}/aggregate_report.html`

---

## Returns

### 最终返回给用户的结构化结果

```json
{
  "eval_id": "web-search-20240312-143022-a1b2c3d4",
  "skill_name": "web-search",
  "eval_profile": "deterministic",
  "total_score": 83,
  "grade": "B",
  "verdict": "PASSED",
  "summary": "综合得分 83/100 (B 级)，各层均已完成",
  "layer_scores": {
    "layer1": 12,
    "layer2_quality": 8,
    "layer2_security": 14,
    "layer4_robust": 7,
    "layer4_correct": 10,
    "layer4_delta": 24
  },
  "main_issues": ["文档缺少 Examples 章节 (-1分)", "部分函数缺少类型注解 (-2分)"],
  "blocked_at": null,
  "report_path": "storage/reports/web-search/web-search-20240312-143022-a1b2c3d4/report.html"
}
```

---

## Examples

### 示例 1：完整评测本地 skill

```
请评测这个 skill：/Users/me/.cursor/skills/my-skill
```
→ 运行 L1–L5，在 `storage/reports/my-skill/{eval_id}/report.html` 生成 HTML 报告，输出分数和等级。

### 示例 2：快速静态检查

```
快速检查一下 /path/to/skill
```
→ 仅运行 L1+L2（无动态测试），返回 score 和问题列表。

### 示例 3：从 GitHub URL 评测

```
评测 https://github.com/user/my-cursor-skill
```
→ 克隆仓库到临时目录，运行完整评测，完成后清理。

### 示例 4：使用本地用例文件（跳过自动生成）

```
评测 /path/to/skill，使用我提供的用例文件 /path/to/my_evals.json
```
→ 跳过 L3 自动生成，直接用 `my_evals.json` 中的测试用例执行 L4 评测。

### 示例 5：使用本地用例 + 自定义评分标准

```
评测 /path/to/skill，用例文件 /path/to/evals.json，评分标准 /path/to/criteria.json
```
→ 同时使用提供的用例和评分标准，`profile_weight_snapshot` 自动从系统配置更新。

### 示例 6：批量评测（命令行）

```bash
python3 -m evaluator.cli evaluate \
  /path/to/skill-a \
  /path/to/skill-b \
  --output-dir ./storage
```
→ 顺序评测每个 skill，输出排名对比表和聚合 HTML 报告。

### 示例 8：触发聚合报告

```
对这几次评测结果生成聚合报告：
eval_id: web-search-20240312-143022-a1b2c3d4
eval_id: calculator-20240312-144500-b2c3d4e5
```
→ 校验 profile_weight_snapshot 一致性，生成 aggregate_report.html。

---

## 评分阈值

| 等级 | 分数区间 | 状态 | 含义 |
|------|---------|------|------|
| A | ≥ 90 | ✅ PASSED | 可直接发布，高质量 |
| B | ≥ 75 | ✅ PASSED | 可发布，建议少量改进 |
| C | ≥ 60 | ✅ PASSED | 可发布，但需改进 |
| D | ≥ 45 | ⚠️ NEEDS_IMPROVEMENT | 不建议发布，需修复 |
| F | < 45 | ❌ FAILED | 存在严重问题 |

**阻断条件（无论分数如何立即失败）**：
- 第一层得分 < `layer1_max × 75%`
- 发现 CRITICAL 安全漏洞
- 全部 P0 核心功能用例健壮性检查均失败

---

## 目录结构

```
skill-evaluator/
├── skill.json                       # 评测框架自身 Skill 元数据
├── SKILL.md                         # 本文件：使用文档
├── CLAUDE.md                        # 项目约定（禁止事项、异步规范等）
├── skill_entry.py                   # Skill 包装层（单个 tool 入口）
├── pyproject.toml                   # 依赖管理
│
├── evaluator/
│   ├── __init__.py
│   ├── config.py                    # 统一配置 + 动态权重
│   ├── pipeline.py                  # 主流程编排
│   ├── cli.py                       # CLI 入口
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── skill.py                 # SkillInfo, SkillMetadata
│   │   ├── evals.py                 # TestCase, ScoringCriteriaConfig 等
│   │   ├── results.py               # TestCaseResult, AggregateData 等
│   │   └── exceptions.py            # 自定义异常体系
│   │
│   ├── layers/
│   │   ├── __init__.py
│   │   ├── base.py                  # BaseLayer 抽象类
│   │   ├── layer1_screening.py      # 第一层：快速筛查
│   │   ├── layer2_static.py         # 第二层：静态分析
│   │   ├── layer3_testgen.py        # 第三层：用例生成 + 评分标准
│   │   ├── layer4_dynamic.py        # 第四层：批跑评估 + I/O 快照
│   │   ├── layer5_report.py         # 第五层：报告生成（强绑定 eval_id）
│   │   └── layer6_aggregate.py      # 聚合层：聚合报告生成
│   │
│   ├── environments/
│   │   ├── __init__.py
│   │   ├── base.py                  # IEnvironmentProvider 接口
│   │   ├── local_env.py             # 本地 venv 环境
│   │   └── docker_env.py            # Docker 沙箱环境
│   │
│   ├── judge/
│   │   ├── __init__.py
│   │   └── llm_judge.py             # LLM-as-Judge 评分器
│   │
│   └── reporters/
│       ├── __init__.py
│       ├── html_reporter.py         # 单次评测 HTML 报告生成器
│       ├── aggregate_reporter.py    # 聚合 HTML 报告生成器
│       └── templates/
│           ├── report.html.j2       # 单次评测 Jinja2 模板
│           └── aggregate.html.j2    # 聚合报告 Jinja2 模板
│
├── storage/                         # 评测产物（已加入 .gitignore）
│   ├── evals/
│   ├── results/
│   ├── reports/
│   └── aggregate/
│
└── tests/
    ├── fixtures/
    └── test_layer*.py
```

---

## 重要约定

### 禁止事项
- 禁止 `print()` / `logging`，统一用 `structlog`
- 禁止裸 dict 层间传数据，用 Pydantic model
- 禁止 `any` 类型
- 禁止硬编码分数线/权重/超时，从 `config.py` 读取
- 禁止同步阻塞 IO，用 `asyncio.create_subprocess_exec`
- 禁止在 layer 内直接调用 Anthropic API，通过 `judge/` 模块
- 禁止 `generator / no_code` 类计算 delta，`delta_score` 固定为 0
- 禁止在 `score_breakdown` 中手填 `max_score`，必须从 `scoring_criteria.json` 的 `profile_weight_snapshot` 读取
- 禁止将 delta 分数并入 correct_max，增值模块（delta）始终作为独立 bonus

### eval_id 绑定规则
- 第三层：`evals.json` 和 `scoring_criteria.json` 共享同一 `eval_id`，写入同一目录
- 第四层：执行前读取 `scoring_criteria.json`，校验 `eval_id` 与当前一致
- 第五层：生成前校验 `scoring_criteria / results / report` 三者 `eval_id` 一致
- `profile_weight_snapshot` 基础项（不含 `delta_max`）之和必须 = 100，不等则抛 `ScoreBindingError`

### 安全红线
命令注入、SQL 注入、代码注入（eval/exec）、硬编码密钥等 CRITICAL 漏洞 → 立即阻断，无例外。

---

## 错误处理

### 阻断消息格式

**第一层阻断**：
```
[BLOCKED at Layer 1] 快速筛查
得分：12/15（阈值：11.25）
问题：
- frontmatter 中缺少 version 字段
- 文档缺少 ## Examples 章节
建议：补充 SKILL.md 的 Examples 章节，在 frontmatter 添加 version 字段
```

**第二层阻断（安全）**：
```
[BLOCKED at Layer 2] 安全红线
发现 CRITICAL 问题：
- [CRITICAL] 命令注入：subprocess.call(user_input, shell=True)
  位置：scripts/processor.py:45
  风险：任意命令执行
处理：修复安全漏洞后再继续评测
```

**第四层阻断（P0 全失败）**：
```
[BLOCKED at Layer 4] 核心功能验证
全部 P0 用例（3/3）健壮性检查均失败
问题：
- tc_001: no_exception 检查失败 - RuntimeError
- tc_002: not_empty 检查失败 - 输出为空
- tc_003: timeout 检查失败 - 超时 60s
建议：检查 skill 运行时环境和依赖安装
```

### 异常体系

```
EvaluationError（基类）
├── BlockedError          评测被阻断（layer, score）
├── SkillInvalidError     Skill 结构不合法
├── EnvError              环境初始化失败
├── JudgeError            LLM Judge 调用失败
├── ScoreBindingError     eval_id 不一致 / max_score 合计 ≠ 100
└── AggregateError        聚合报告生成失败
```

---

## 最佳实践

### 对被评测 Skill 的开发者
1. 确保 `SKILL.md` 有完整 frontmatter（`name`、`description`、`version`、`type`、`author`）
2. 在 `## Examples` 章节提供输入/输出对照
3. 如可能，预先提供 `evals/evals.json` 测试用例
4. 本地先运行 `pylint` 和 `bandit` 发现明显问题

### 高质量 Skill 指标
- 第一层得分 ≥ `layer1_max × 90%`
- 无安全问题
- 代码质量得分 ≥ `quality_max × 80%`
- 功能通过率 ≥ 80%
- `delta_raw` 为正值（skill 优于基线）

### 红旗警告
- `delta_raw ≤ 0`（skill 无增量价值）
- 发现 CRITICAL 安全漏洞
- Token 用量比基线高 3x 但通过率无提升
- 所有 P0 用例均失败
