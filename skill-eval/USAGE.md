# Skill 评测框架 — 使用说明

> 版本 1.0.0 | 最后更新 2026-03-30

## 项目概述

Skill Evaluator v1.0 是通用 AI Skill / Agent / 工具能力综合质量评测框架。对任意 Cursor Skill 进行五层自动化评测：元数据合规 → 静态代码扫描 → 测试用例生成 → with/without-skill 对比执行 → HTML 报告生成，并支持同类 skill 聚合对比报告。

**核心升级（相比 v1.0）**：
- 完整 `eval_id` 绑定机制，评测产物强一致性校验
- with/without-skill 基线对比，量化 skill 增量价值
- 统一 `storage/` 存储规范，结果与 skill 目录解耦
- `evaluator/` 模块化包结构，替代 `scripts/` 脚本集合
- 动态评分标准（`scoring_criteria.json`）由 LLM 针对每个用例生成

---

## 目录结构

```
skill-evaluator/
├── skill.json                       # Skill 元数据（供 Cursor 激活）
├── SKILL.md                         # 完整 API 文档（触发条件、参数、示例）
├── USAGE.md                         # 本文档（详细使用说明）
├── README.md                        # 快速入门
├── CLAUDE.md                        # 架构约定与禁止事项
├── pyproject.toml                   # 依赖管理（Poetry）
├── skill_entry.py                   # Skill 包装层（单个 tool 统一入口）
│
├── evaluator/
│   ├── config.py                    # 统一配置 + 动态权重（ScoreProfile）
│   ├── pipeline.py                  # 主流程编排
│   ├── cli.py                       # CLI 入口（click，命令：skill-eval）
│   ├── models/                      # Pydantic 数据模型（层间共享）
│   │   ├── skill.py                 # SkillInfo, SkillMetadata
│   │   ├── evals.py                 # TestCase, ScoringCriteriaConfig 等
│   │   ├── results.py               # TestCaseResult, AggregateData 等
│   │   └── exceptions.py            # 自定义异常体系
│   ├── layers/                      # 各评测层（继承 BaseLayer）
│   │   ├── layer1_screening.py      # L1：快速筛查
│   │   ├── layer2_static.py         # L2：静态分析
│   │   ├── layer3_testgen.py        # L3：用例生成 + 评分标准
│   │   ├── layer4_dynamic.py        # L4：批跑评估 + I/O 快照
│   │   ├── layer5_report.py         # L5：报告生成（强绑定 eval_id）
│   │   └── layer6_aggregate.py      # 聚合层：聚合报告生成
│   ├── environments/                # 执行环境提供者
│   │   ├── local_env.py             # 本地 venv 环境
│   │   └── docker_env.py            # Docker 沙箱环境
│   ├── judge/
│   │   └── llm_judge.py             # LLM-as-Judge 评分器
│   └── reporters/
│       ├── html_reporter.py         # 单次评测 HTML 报告生成器
│       ├── aggregate_reporter.py    # 聚合 HTML 报告生成器
│       └── templates/
│           ├── report.html.j2       # 单次报告 Jinja2 模板（禁止覆盖）
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

## 安装

### 依赖管理（Poetry）

```bash
# 基础安装（运行评测框架）
poetry install

# 含静态分析工具（pylint / radon / bandit / pip-audit）
poetry install --with static-analysis

# 含 Docker 沙箱支持
poetry install --with docker

# 全部依赖
poetry install --with static-analysis,docker
```

> **注意**：静态分析工具（pylint、bandit 等）为可选依赖。未安装时，L2 自动降级为 LLM 推理评估，报告中会标注 `evaluation_method: "LLM推理"`。

### Python 版本要求

Python 3.10+（与 `pyproject.toml` 保持一致，推荐 3.11+）。

---

## CLI 参考

安装后可使用 `skill-eval` 命令，也可通过 `python3 -m evaluator.cli` 调用。

### 完整评测（五层 + 可选聚合）

```bash
skill-eval evaluate /path/to/skill
```

### 快速静态检查（L1 + L2，适合 CI/CD）

```bash
skill-eval evaluate /path/to/skill --mode=quick
```

### 批量评测

```bash
skill-eval evaluate \
  /path/to/skill-a \
  /path/to/skill-b \
  /path/to/skill-c \
  --output-dir ./storage
```

### 从 GitHub URL 评测

```bash
skill-eval evaluate https://github.com/owner/skill-repo
```

> 自动 `git clone` 到临时目录，评测完成后自动清理。私有仓库请先手动 clone 到本地路径。

### 触发聚合报告

```bash
skill-eval aggregate \
  --eval-ids "skill-a-20260323-120000-abc12345,skill-b-20260323-121500-def67890"
```

### 使用本地用例文件（跳过自动生成）

```bash
# 提供本地用例文件，跳过 L3 自动生成
skill-eval evaluate /path/to/skill --evals-file /path/to/my_evals.json

# 同时提供用例文件和评分标准文件
skill-eval evaluate /path/to/skill \
  --evals-file /path/to/my_evals.json \
  --criteria-file /path/to/my_criteria.json

# 只提供评分标准（用例自动生成，评分规则用文件中的）
skill-eval evaluate /path/to/skill --criteria-file /path/to/my_criteria.json
```

### 所有可用选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--mode` | `full` | `full`（全五层）/ `quick`（L1+L2）/ `custom`（指定层） |
| `--env` | `auto` | `auto`（优先 Docker）/ `docker` / `local` |
| `--output` | `both` | `html` / `markdown` / `both` |
| `--aggregate` | `false` | 完成后是否触发聚合报告 |
| `--output-dir` | `./storage` | 批量评测时的输出根目录 |
| `--evals-file` | — | 本地用例文件路径（见《本地文件使用说明》） |
| `--criteria-file` | — | 本地评分标准文件路径（见《本地文件使用说明》） |
| `--judge-model` | `JUDGE_MODEL` 环境变量 | 覆盖 Judge 评分使用的 LLM（见《评测 LLM 配置》） |
| `--eval-model` | `EVAL_MODEL` 环境变量 | 覆盖测试用例执行使用的 LLM（见《评测 LLM 配置》） |
| `--with-baseline` | `false` | 启用 without-skill 基线对比（增量价值评分），默认关闭以节省 token。启用后 `deterministic`/`workflow` 的增量价值恢复为 30 分，正确性相应减少 |

---

## 评测层次说明

| 层次 | 名称 | 耗时 | 满分（按 profile） | 阻断条件 |
|------|------|------|---------------------|---------|
| L1 | 快速筛查 | < 30 秒 | `layer1_max`（15 或 20） | 得分 < `layer1_max × 75%` |
| L2 | 静态分析 | < 30 秒 | `quality_max + security_max` | 发现 CRITICAL 安全漏洞 |
| L3 | 用例生成 | 1–2 分钟 | — （不直接计分） | — |
| L4 | 批跑评估 | 2–10 分钟 | `robust_max + correct_max + delta_max` | 全部 P0 用例健壮性检查失败 |
| L5 | 报告生成 | < 10 秒 | — | eval_id 不一致 / 权重快照异常 |
| 聚合 | 聚合报告 | < 30 秒 | — | 权重快照不一致 / 评测数 < 2 |

### 等级判定

| 等级 | 分数区间 | 状态 | 含义 |
|------|---------|------|------|
| A | ≥ 90 | ✅ PASSED | 可直接发布，高质量 |
| B | ≥ 75 | ✅ PASSED | 可发布，建议少量改进 |
| C | ≥ 60 | ✅ PASSED | 可发布，但需改进 |
| D | ≥ 45 | ⚠️ NEEDS_IMPROVEMENT | 不建议发布，需修复 |
| F | < 45 | ❌ FAILED | 存在严重问题 |

### L2 安全红线（以下任一触发即强制阻断）

- 命令注入（`os.system`、`subprocess.call(shell=True)`）
- 代码注入（`eval()`、`exec()`）
- 硬编码密钥（`sk-xxx`、`password =` 等）
- SQL 注入风险

---

## Profile 系统与动态权重

`eval_profile` 由 `skill.json` 的 `type` 字段 + 是否有代码文件共同决定，影响各层满分上限：

| Profile | L1 基础合规 | L2 代码质量 | L2 安全合规 | L4 健壮性 | L4 正确性 | L4 增量价值 | 合计 |
|---------|:-----------:|:-----------:|:-----------:|:---------:|:---------:|:-----------:|:----:|
| `deterministic` | 15 | 15 | 20 | 8 | 42 | 0 | **100** |
| `generative` | 15 | 5 | 15 | 10 | 55 | 0 | **100** |
| `workflow` | 15 | 10 | 15 | 8 | 52 | 0 | **100** |
| `no_code` | 20 | 0 | 10 | 15 | 55 | 0 | **100** |

> **注意**：`deterministic` 和 `workflow` 的增量价值（L4 增量价值）默认为 0，原 30 分权重已并入正确性。使用 `--with-baseline` 参数启用基线对比后，增量价值恢复为 30 分，正确性相应减少。

### Profile 推断链（按优先级）

1. `skill.json` 或 SKILL.md frontmatter 有明确 `type` → 直接使用
2. 无代码文件 → `no_code`
3. 有代码 + 描述含 `workflow/pipeline/orchestrat/工作流/流程/编排/多步骤/链式/阶段` → `workflow`
4. 有代码 + 描述含 `generat/creat/write/produc/draft/生成/创作/撰写/产出/起草/输出文档` → `generative`
5. 有代码 + 描述含 `analyz/extract/classif/detect/parse/evaluat/分析/提取/分类/检测/解析/评估/审查` → `deterministic`（analyzer）
6. 有代码，无关键词匹配 → `deterministic`（tool）

> 推断结果记录 `type_inferred = True`，并在 L1 扣合规分（`layer1_max × 5%`）。

### 各 Profile 测试策略

**`no_code`**（无代码文件）：
- L2 代码质量跳过，授予 `quality_max` 满分
- L4 健壮性使用文档检查类型：`doc_coverage`、`param_valid`、`example_match`、`logic_coherent`
- 增量价值固定为 0，跳过 without-skill 运行

**`deterministic`**（tool / analyzer）：
- L2 运行 `pylint`（错误/警告比例）、`radon cc`（圈复杂度 > 10 扣分）、类型注解覆盖率
- L4 健壮性：`no_exception`、`not_empty`、`timeout`、`exit_code`、`contains_field`
- L4 增量价值：默认为 0（节省 token）。使用 `--with-baseline` 启用后计算：`delta_score = max(0, with_correct - without_correct + 0.5) × delta_max`，其中 `delta_max` 恢复为 30

**`generative`**（生成类）：
- L4 正确性权重最高（55 分），侧重输出质量评估
- 增量价值固定为 0（生成类无稳定基线可对比）

**`workflow`**（流程编排类）：
- L4 验证 pipeline 各步骤按序完成
- 增量价值：默认为 0（节省 token）。使用 `--with-baseline` 启用后有效（30 分），验证 skill 在流程协调上的提升

---

## 存储结构

所有评测产物统一存储到 `storage/`，与被评测 skill 目录完全解耦：

```
storage/
├── evals/
│   └── {skill_name}/
│       └── {eval_id}/
│           ├── evals.json                  # 测试用例集（P0/P1/P2）
│           └── scoring_criteria.json       # 动态评分标准（与 evals.json 同步生成）
│
├── results/
│   └── {skill_name}/
│       └── {eval_id}/
│           ├── with_skill/
│           │   └── {tc_id}.json            # 执行快照（有 skill）
│           └── without_skill/
│               └── {tc_id}.json            # 执行快照（无 skill 基线）
│
├── reports/
│   └── {skill_name}/
│       └── {eval_id}/
│           ├── eval_data.json              # 结构化评测数据
│           └── report.html                 # HTML 可视化报告
│
└── aggregate/
    └── {profile_type}/
        └── {aggregate_id}/
            ├── aggregate_data.json
            └── aggregate_report.html
```

**ID 格式规范**：
- `eval_id`：`{skill_name}-{YYYYMMDD-HHMMSS}-{uuid4前8位}`，例如 `my-skill-20260323-143022-a1b2c3d4`
- `aggregate_id`：`{profile_type}-aggregate-{YYYYMMDD-HHMMSS}-{uuid4前8位}`

> `storage/` 已加入 `.gitignore`，不提交到版本控制。

---

## 单次评测输出说明

### 查看 HTML 报告

```bash
open storage/reports/<skill-name>/<eval-id>/report.html
```

### eval_data.json 结构概览

```json
{
  "eval_id": "my-skill-20260323-143022-a1b2c3d4",
  "skill_name": "my-skill",
  "eval_profile": "deterministic",
  "total_score": 83,
  "grade": "B",
  "verdict": "PASSED",
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
  "report_path": "storage/reports/my-skill/my-skill-20260323-143022-a1b2c3d4/report.html"
}
```

---

## 批量评测

### 基本用法

```bash
# 批量评测多个 skill（空格分隔路径）
skill-eval evaluate \
  /path/to/skill-a \
  /path/to/skill-b \
  /path/to/skill-c \
  --output-dir ./storage

# 从文件读取 skill 路径列表
skill-eval evaluate \
  --file skill_list.txt \
  --output-dir ./storage
```

### Skill 列表文件格式（`--file`）

```
# 支持注释（# 开头）和空行
/Users/mia/.cursor/skills/my-skill
https://github.com/owner/repo-skill
/tmp/another-skill
```

### 批量评测输出

各 skill 的报告独立存入 `storage/reports/{skill_name}/{eval_id}/`。聚合报告存入：

```
storage/aggregate/{profile_type}/{aggregate_id}/
├── aggregate_data.json
└── aggregate_report.html
```

**聚合报告包含**：
- 批次总览（总数 / 通过 / 阻断 / 平均分）
- Skill 排名表（名称、Profile、分数、等级、分项可视化）
- 维度对比矩阵（各 skill 在各评测维度上的横向对比）

> **聚合触发条件**：所有被聚合的 `eval_id` 必须 `eval_profile` 相同 且 `profile_weight_snapshot` 完全一致。

---

## 在 Claude 中使用（Skill 模式）

框架封装为单个 Skill tool，Claude 加载后可直接对话触发：

```
请评测这个 skill：/Users/me/.cursor/skills/my-skill
```

```
快速检查一下 /path/to/skill，只看静态问题
```

```
评测 https://github.com/user/my-cursor-skill
```

触发条件：用户说「评测这个 skill」「给这个 skill 打分」「检查 skill 质量」「安全扫描」「批量评测」，或提供 skill 路径请求质量检测。

---

## 评测 LLM 配置

### 两个独立的 LLM 角色

框架在 L4 阶段使用**两个** LLM，职责完全不同，可独立配置：

| 角色 | 配置项 | 默认值 | 用途 |
|------|--------|--------|------|
| **Judge 模型** | `--judge-model` / `JUDGE_MODEL` | `claude-opus-4-5` | 对 with-skill 和 without-skill 的输出打分（正确性评估），需要 API Key |
| **Eval 执行模型** | `--eval-model` / `EVAL_MODEL` | `claude-opus-4-5` | 仅在 LLM API 路径下执行测试用例（Claude CLI 路径不受此控制） |

### L4 执行路径优先级

L4 动态评测按以下优先级自动选择执行路径：

| 优先级 | 路径 | 使用的模型 | 条件 |
|--------|------|-----------|------|
| 1 | **Claude CLI** | CLI 本地配置的默认模型 | 系统已安装 `claude` CLI |
| 2 | **Anthropic API** | `EVAL_MODEL` | 有 `ANTHROPIC_API_KEY` |
| 3 | **OpenAI 兼容 API** | `EVAL_MODEL` 或端点默认模型 | 有 `OPENAI_API_KEY` |
| 4 | **本地入口点** | — | 直接运行 skill 的 `skill_entry.py` |

> **重要**：当走 Claude CLI 路径时，`EVAL_MODEL` 配置**不生效**。CLI 使用的是本地 `claude` 命令配置的默认模型。skill 通过沙箱隔离执行（临时目录 + SKILL.md 复制 + system prompt 注入）。

### 评判（Scoring）降级机制

评判阶段**独立于执行路径**，按以下优先级选择评判方式：

| 优先级 | 方式 | 条件 | 精度 |
|--------|------|------|------|
| 1 | **程序化检查** | `deterministic`/`workflow` profile 且置信度 ≥ 0.7 | 高（结构化字段校验） |
| 2 | **LLM Judge** | 有 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY` | 高（语义评估） |
| 3 | **规则匹配 fallback** | 无任何 API Key | 低（关键词匹配） |

> **注意**：如果没有配置任何 API Key，`JUDGE_MODEL` 不会生效，评判将降级为基于关键词的规则匹配（`eval_method: "rule_based"`），评分精度较低。建议至少配置一个 API Key 以获得准确的评判结果。

### 配置方式（优先级从高到低）

```
CLI 参数 > 环境变量 / .env 文件 > 代码默认值
```

**方式 1：CLI 参数（推荐，仅对本次评测生效）**

```bash
# 使用 GPT-4o 作为 Judge，claude-opus-4-5 执行测试用例
skill-eval evaluate /path/to/skill --judge-model gpt-4o

# 两个模型都指定
skill-eval evaluate /path/to/skill \
  --judge-model gpt-4o \
  --eval-model claude-haiku-4-5
```

**方式 2：环境变量（对当前 shell 会话生效）**

```bash
export JUDGE_MODEL=gpt-4o
export EVAL_MODEL=claude-sonnet-4-5
skill-eval evaluate /path/to/skill
```

**方式 3：.env 文件（对项目全局生效）**

在项目根目录创建或编辑 `.env`：

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...          # 如需 OpenAI 兼容模型
OPENAI_BASE_URL=               # 可选，自定义 OpenAI 兼容端点

JUDGE_MODEL=gpt-4o
EVAL_MODEL=claude-opus-4-5
```

**可选运行参数（不填则使用默认值）**：

```dotenv
STORAGE_BASE_DIR=./storage
LAYER3_TIMEOUT=120
LAYER4_CASE_TIMEOUT=60
LAYER4_TOTAL_TIMEOUT=600
JUDGE_PASSING_THRESHOLD=0.7
DELTA_NORMALIZE_OFFSET=0.5  # 仅在使用 --with-baseline 时生效
```

### Provider 自动选择

框架根据已配置的 API Key 自动选择 Provider，无需手动指定：

| 已设置的 Key | 使用的 Provider |
|-------------|----------------|
| `ANTHROPIC_API_KEY` 有效 | Anthropic API |
| 仅 `OPENAI_API_KEY` 有效 | OpenAI / 兼容端点 |
| 两者都有 | 优先 Anthropic |
| 两者都没有 + 有 Claude CLI | 执行走 CLI，评判降级为 `rule_based` |
| 两者都没有 + 无 Claude CLI | 报错：`No LLM provider configured` |

> **注意**：模型名称需与所选 Provider 匹配。例如，设置 `--judge-model gpt-4o` 时需确保 `OPENAI_API_KEY` 已配置。即使有 Claude CLI 可执行测试用例，评判仍需 API Key 才能使用 LLM Judge。

### 常见模型配置示例

```bash
# 低成本快速评测（Haiku 执行 + Haiku Judge）
skill-eval evaluate /path/to/skill \
  --judge-model claude-haiku-4-5 \
  --eval-model claude-haiku-4-5

# 高质量评测（Sonnet 执行 + Opus Judge）
skill-eval evaluate /path/to/skill \
  --judge-model claude-opus-4-5 \
  --eval-model claude-sonnet-4-5

# 使用 OpenAI 兼容端点（如 GLM-4）
OPENAI_API_KEY=your-key OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \
skill-eval evaluate /path/to/skill \
  --judge-model glm-4 \
  --eval-model glm-4
```

---

## 本地文件使用说明

### 概述

L3（测试用例生成 + 评分标准生成）支持两个独立的文件覆盖选项：

| 选项 | 效果 | 不提供时 |
|------|------|---------|
| `--evals-file` | 使用文件中的测试用例，跳过自动生成 | 从 SKILL.md 自动生成 |
| `--criteria-file` | 使用文件中的评分规则，跳过自动生成 | 从测试用例自动生成 |

两个选项完全独立，可任意组合。

---

### `--evals-file`：本地用例文件

**支持格式**：

```json
// 格式 A：完整 evals.json（有 test_cases 字段）
{
  "test_cases": [
    {
      "id": "tc_001",
      "priority": "P0",
      "prompt": "请评测这个 skill：/path/to/skill",
      "expected_behavior": "返回完整评测报告，包含分数和等级"
    }
  ]
}

// 格式 B：裸 JSON 数组（直接放 test case 列表）
[
  {
    "prompt": "请评测这个 skill：/path/to/skill",
    "priority": "P0",
    "expected_behavior": "返回完整评测报告"
  }
]
```

**字段说明**：

| 字段 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt` | 是 | — | 测试用的用户 prompt（无 prompt 的条目会被忽略） |
| `id` | 否 | `tc_001`, `tc_002`… | 用例 ID，缺失时自动按序生成 |
| `priority` | 否 | `P0` | 优先级：`P0` / `P1` / `P2` |
| `expected_behavior` | 否 | `""` | 期望行为描述（供 LLM Judge 参考） |
| `robustness_checks` | 否 | 按 profile 自动填充 | 健壮性检查规则列表 |
| `correctness_rubric` | 否 | `[]` | 正确性评分维度列表 |
| `baseline_prompt` | 否 | 自动生成 | without-skill 基线 prompt（仅在使用 `--with-baseline` 时用于 delta 评分） |
| `context` | 否 | `{}` | 附加上下文 |

> **注意**：`source` 字段会被强制设为 `"manual"`，无论文件中写的是什么。

---

### `--criteria-file`：本地评分标准文件

**支持格式**：

```json
// 格式 A：完整 scoring_criteria.json（有 criteria_by_tc 字段）
{
  "criteria_by_tc": [
    {
      "tc_id": "tc_001",
      "robustness_scoring": [...],
      "correctness_scoring": [
        {
          "assertion_id": "c_001",
          "criterion": "返回结果包含 total_score 字段",
          "weight": 3.0,
          "score_levels": {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0},
          "scoring_guidance": "检查输出中是否存在 total_score 数值字段，范围 0-100"
        }
      ],
      "delta_scoring": {...}  # 仅在使用 --with-baseline 时生效
    }
  ]
}

// 格式 B：裸 JSON 数组（直接放 per-tc 标准列表）
[
  {
    "tc_id": "tc_001",
    "correctness_scoring": [...]
  }
]
```

**匹配规则（按 tc_id）**：

- 文件中的 `tc_id` 与当前用例集的 `id` 一致 → 使用文件中的评分规则
- 用例有 `id` 但文件中没有对应条目 → 自动生成该用例的评分标准
- 文件中的 `tc_id` 找不到对应用例 → 忽略并记录 warning

**重要约束**：`profile_weight_snapshot`（各层满分上限）始终从系统当前配置重新写入，文件中的值不生效。这是为了保证 `score_breakdown` 的一致性。

---

### 推荐工作流

**场景 A：首次评测，获取用例模板，再自定义**

```bash
# 第一步：自动生成用例（完整评测一次）
skill-eval evaluate /path/to/skill

# 第二步：复制生成的用例文件，按需修改
cp storage/evals/my-skill/my-skill-20260323-xxx/evals.json my_custom_evals.json

# 第三步：用自定义用例重新评测
skill-eval evaluate /path/to/skill --evals-file my_custom_evals.json
```

**场景 B：固定用例集，每次评测用统一标准**

```bash
# 维护一套固定的用例和评分标准文件，用于持续回归
skill-eval evaluate /path/to/skill \
  --evals-file tests/regression_evals.json \
  --criteria-file tests/regression_criteria.json
```

**场景 C：调整评分标准，保持用例不变**

```bash
# 只修改评分标准（例如提高某个维度权重），用例自动生成
skill-eval evaluate /path/to/skill --criteria-file my_strict_criteria.json
```

---

## 常见问题

### Q：`no_code` skill 的 L2 代码质量如何计分？

`no_code` 完全跳过代码质量检查（`quality_max = 0`），直接授予满分。安全合规分为 10 分满分，总分仍为 100 分。

### Q：分数总和看起来不是 100？

总分上限始终是 100 分（由 `profile_weight_snapshot` 各项之和保证，框架校验失败会抛 `ScoreBindingError`）。若 skill 被阻断，只显示阻断前已完成层次的得分，总分偏低属预期行为。

### Q：为何不同 profile 的 skill 测试用例差异很大？

这是正确行为。框架针对每种 profile 使用不同测试策略：
- `no_code` → 分析 SKILL.md 文档质量（覆盖度、参数规范、示例质量）
- `deterministic` → 运行实际代码，验证结构化输出和健壮性
- `generative` → 侧重输出质量和正确性的 LLM Judge 评估
- `workflow` → 验证 pipeline 各步骤是否按序完成

### Q：评测结果在哪里找？

- 单次报告：`storage/reports/<skill-name>/<eval-id>/report.html`
- 批量聚合：`storage/aggregate/<profile_type>/<aggregate-id>/aggregate_report.html`

### Q：静态分析工具没安装会怎样？

`pylint`、`radon`、`bandit`、`pip-audit` 均为可选依赖。未安装时，L2 自动降级为 LLM 推理评估，报告中标注 `evaluation_method: "LLM推理"`，分数仍正常计算。

### Q：如何从已有的 eval_id 生成新报告？

第五层（L5）可独立重跑：

```bash
skill-eval report --eval-id <eval_id> --skill-name <skill_name>
```

### Q：L4 报告中的 `max_iterations=8` 失败专栏是什么意思？

表示该用例在工具调用循环中达到了上限轮次（默认 8 次）仍未拿到最终稳定答案。报告会将这类用例单独列出，并继续按执行路径分层展示，方便快速定位是 `openapi` 直连、bridge 降级还是其他路径触发。

### Q：提供了 `--criteria-file` 但 tc_id 全部对不上，会怎样？

框架会 warning 日志提示 `layer3.criteria_file_no_matches`，然后对所有用例自动生成评分标准（相当于 `--criteria-file` 未生效）。报告中 `criteria_source` 仍然会记录文件路径，方便排查。

解决方法：确保 `criteria_file` 中的 `tc_id` 与用例的 `id` 字段一致（通常是 `tc_001`, `tc_002`…），或配合 `--evals-file` 一起使用。

### Q：什么情况会触发 `ScoreBindingError`？

- `eval_id` 不一致：`scoring_criteria.json` 和 `results/` 目录的 eval_id 不匹配
- `profile_weight_snapshot` 各项之和 ≠ 100
- `score_breakdown` 中手填了 `max_score`（应从快照读取）

---

## 对 Skill 开发者的建议

### 提高评测分数的关键项

1. **补全 SKILL.md**：必须有 `## Description`、`## Parameters`、`## Examples`、`## Returns` 四个章节，Examples 要有输入/输出对照
2. **完善 frontmatter**：`name`（kebab-case）、`version`（semver）、`type`、`description`（≥ 20 字符）、`author` 缺一不可
3. **本地预检**：提交前跑 `pylint` 和 `bandit`，修复 CRITICAL/HIGH 问题
4. **类型注解**：Python 代码尽量标注函数入参和返回值类型

### 高质量 Skill 指标

- L1 得分 ≥ `layer1_max × 90%`（无元数据缺失）
- 无安全问题
- L2 代码质量得分 ≥ `quality_max × 80%`
- L4 功能通过率 ≥ 80%
- 使用 `--with-baseline` 时 `delta_raw > 0`（skill 使用后优于基线）

### 红旗警告

- 使用 `--with-baseline` 时 `delta_raw ≤ 0`：skill 无增量价值，用了不如不用
- 发现 CRITICAL 安全漏洞：直接阻断，无例外
- Token 用量比基线高 3x 但通过率无提升
- 全部 P0 用例均失败

---

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|---------|
| 2.5.0 | 2026-03-29 | 评分框架重构：without-skill 基线对比默认关闭（`delta_max=0`），原 delta 分数并入 `correct_max`（`deterministic`: 42, `workflow`: 52）；新增 `--with-baseline` CLI 参数启用基线对比，节省默认评测 token 消耗 |
| 2.4.0 | 2026-03-29 | L4 `failure_reason` 修复（成功用例不再显示 `simulation_note`）；移除 MCP bridge 路径；新增执行路径优先级和评判降级机制文档；Claude CLI 沙箱隔离说明 |
| 2.3.0 | 2026-03-26 | L4 报告新增 `max_iterations=8` 失败用例专栏（按执行路径分层）；文档统一补充安装与环境配置说明 |
| 2.2.0 | 2026-03-23 | `--judge-model` / `--eval-model` CLI 选项：覆盖评测 LLM，仅对单次评测生效；`LLMJudge` 新增 `_judge_model` / `eval_model` property，Layer4 统一通过 `judge.eval_model` 读取，不再直接访问 settings |
| 2.1.0 | 2026-03-23 | L3 支持 `--evals-file`（本地用例文件）和 `--criteria-file`（本地评分标准文件）；`_normalize_test_case` 自动补全缺失字段；criteria 文件按 tc_id 匹配，无匹配条目自动降级为自动生成 |
| 2.0.0 | 2026-03-23 | 完整重构：`evaluator/` 模块化包；五层 + 聚合架构；eval_id 强绑定；with/without-skill 基线对比（增量价值量化）；动态评分标准（scoring_criteria.json）；storage/ 统一存储规范；等级阈值更新（B≥75, C≥60, D≥45）；`skill-eval` CLI 入口；Poetry 依赖管理 |
| 1.0.0 | 2026-03-15 | L4 重构：profile 感知差异化测试；知识库初始化；批量评测 + 聚合报告；全文档汉化 |
| 0.9.x | 2026-03-14 | L4 动态评测上线；自动 HTML 报告生成；`--mode=quick` 支持 |
| 0.8.x | 2026-03-13 | eval_data.json 数据模型完善；报告模板重构 |
| 0.7.x | 2026-03-12 | 初始多层评测框架；L1/L2/L3 实现 |

## 最新更新

- **L5 报告新增「增量价值分析」独立 section**：启用 `--with-baseline` 后，HTML 报告中新增独立的增量价值分析卡片，包含 verdict 判定横幅、耗时 & Token 总计对比、逐用例对比明细表。
- **`regenerate-report` 命令增强**：分层检查 L1/L2/L4 数据完整性，缺失的层自动重新运行。
