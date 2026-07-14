# Skill 质量评测框架 v1.0

通用 AI Skill / Agent / 工具能力综合质量评测系统，实现五层自动化测试、静态代码扫描与 with/without-skill 基线对比，输出 0-100 分评分（A-F 等级）和 HTML 可视化报告。

> **完整使用说明请见 [USAGE.md](USAGE.md)**
>
> 最后更新：2026-03-30

---

## 总览

五层 + 聚合评测架构，每层有明确阻断条件：

| 层次 | 名称 | 耗时 | 说明 |
|------|------|------|------|
| L1 | 快速筛查 | < 30 秒 | 元数据合法性、SKILL.md 结构、基础合规 |
| L2 | 静态分析 | < 30 秒 | 代码质量（pylint/radon）+ 安全扫描（bandit/pip-audit） |
| L3 | 用例生成 | 1–2 分钟 | 从 SKILL.md 自动生成 evals.json + scoring_criteria.json |
| L4 | 批跑评估 | 2–10 分钟 | with/without-skill 对比执行，完整保存 I/O 快照 |
| L5 | 报告生成 | < 10 秒 | 强绑定 eval_id，生成 eval_data.json + report.html |
| 聚合 | 聚合报告 | < 30 秒 | 同类 skill 横向对比（≥ 2 次评测触发） |

---

## 安装

### 推荐方式：虚拟环境 + requirements.txt（适用所有系统）

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

### 可选方式：Poetry（开发和贡献者推荐）

```bash
poetry install
# 如需静态分析工具
poetry install --with static-analysis
```

### 验证安装

```bash
python3 -m evaluator.cli --help
```

### 常见报错 & 解决

| 报错 | 原因 | 解决方案 |
|------|------|---------|
| `OSError: [Errno 1] Operation not permitted: '.../Python/3.13'` | macOS Python 3.12+ 外部管理限制 | 使用 venv（推荐）或加 `--break-system-packages` |
| `error: externally-managed-environment` | 同上 | `pip install --break-system-packages -r requirements.txt` |
| `ModuleNotFoundError: No module named 'evaluator'` | 未在 skill-evaluator 目录执行 | `cd skill-evaluator` 后再运行 |

> **macOS Python 3.13 用户注意**：直接 `pip install` 会失败。请务必先创建 venv，或使用 `pyenv` 安装一个独立 Python 环境。

---

## 快速开始

### 方式一：在 Cursor 中对话触发（推荐）

安装本 skill 后，直接在对话框中说：

```
评测这个 skill：/Users/me/.cursor/skills/my-skill
```

```
快速检查一下 /path/to/skill 的代码质量和安全问题
```

```
给 https://github.com/user/my-cursor-skill 打个分，看看质量怎么样
```

```
评测 /path/to/skill，用我提供的测试用例 /path/to/my_evals.json
```

### 方式二：CLI 命令行

```bash
# 完整评测（推荐）
python3 -m evaluator.cli evaluate /path/to/skill

# 快速静态检查（L1 + L2，适合 CI/CD，< 1 分钟）
python3 -m evaluator.cli evaluate /path/to/skill --mode=quick

# 批量评测多个 skill
python3 -m evaluator.cli evaluate \
  /path/to/skill-a \
  /path/to/skill-b \
  --output-dir ./storage

# 从 GitHub URL 评测（自动 clone）
python3 -m evaluator.cli evaluate https://github.com/owner/skill-repo

# 使用自定义测试用例（跳过 L3 自动生成）
python3 -m evaluator.cli evaluate /path/to/skill \
  --evals-file /path/to/my_evals.json

# 启用 without-skill 基线对比（增量价值评分）
python3 -m evaluator.cli evaluate /path/to/skill --with-baseline
```

---

## 使用示例（5 个典型场景）

### 场景 1：首次评测一个本地 skill

```bash
python3 -m evaluator.cli evaluate /Users/me/.cursor/skills/my-skill
```

输出示例：
```
✅ my-skill — Score: 83/100  Grade: B (PASSED)
📊 Report: storage/reports/my-skill/my-skill-20260324-143022-a1b2c3d4/report.html
```

然后查看报告：
```bash
open storage/reports/my-skill/my-skill-20260324-143022-a1b2c3d4/report.html
```

---

### 场景 2：CI/CD 快速静态扫描（< 1 分钟）

只运行 L1 + L2（元数据合规 + 代码安全扫描），无需等待 LLM 测试：

```bash
python3 -m evaluator.cli evaluate /path/to/skill --mode=quick
```

适用于 PR 检查、发布前把关。

---

### 场景 3：评测 GitHub 上的 skill

```bash
python3 -m evaluator.cli evaluate https://github.com/user/my-cursor-skill
```

框架自动 `git clone` 到临时目录，评测完成后自动清理。

---

### 场景 4：用固定测试用例做回归测试

先让框架自动生成用例模板，再固定下来：

```bash
# 第一步：完整评测一次，生成用例
python3 -m evaluator.cli evaluate /path/to/skill

# 第二步：复制用例文件备用
cp storage/evals/my-skill/my-skill-xxx/evals.json tests/regression_evals.json

# 第三步：之后每次用固定用例评测
python3 -m evaluator.cli evaluate /path/to/skill \
  --evals-file tests/regression_evals.json
```

---

### 场景 5：使用不同 LLM 进行评测

```bash
# 低成本快速评测（Haiku 执行 + Haiku Judge）
python3 -m evaluator.cli evaluate /path/to/skill \
  --judge-model claude-haiku-4-5 \
  --eval-model claude-haiku-4-5

# 高质量评测（Sonnet 执行 + Opus Judge）
python3 -m evaluator.cli evaluate /path/to/skill \
  --judge-model claude-opus-4-5 \
  --eval-model claude-sonnet-4-5
```

---

## 评分说明

**总分 100 分**，各维度分值因 `eval_profile` 不同：

| Profile | L1 基础合规 | L2 代码质量 | L2 安全合规 | L4 健壮性 | L4 正确性 | L4 增量价值 | 合计 |
|---------|:-----------:|:-----------:|:-----------:|:---------:|:---------:|:-----------:|:----:|
| `deterministic` | 15 | 15 | 20 | 8 | 42 | 0 | **100** |
| `generative` | 15 | 5 | 15 | 10 | 55 | 0 | **100** |
| `workflow` | 15 | 10 | 15 | 8 | 52 | 0 | **100** |
| `no_code` | 20 | 0 | 10 | 15 | 55 | 0 | **100** |

> `no_code` 完全跳过代码质量检查，不扣分。  
> `generative` / `no_code` 增量价值固定为 0，跳过 without-skill 运行。

**等级划分**：

| 等级 | 分数 | 状态 |
|------|------|------|
| A | ≥ 90 | ✅ PASSED |
| B | ≥ 75 | ✅ PASSED |
| C | ≥ 60 | ✅ PASSED |
| D | ≥ 45 | ⚠️ NEEDS_IMPROVEMENT |
| F | < 45 | ❌ FAILED |

---

## 安全红线（立即阻断）

以下情况无论其他分数如何，立即触发阻断：

- 命令注入（`shell=True`、`os.system`）
- 代码注入（`eval`、`exec`）
- 硬编码密钥（API key、password 等）
- SQL 注入

---

## 输出文件

评测产物统一存储到 `storage/`：

```
storage/
├── evals/{skill_name}/{eval_id}/
│   ├── evals.json                  # 测试用例集
│   └── scoring_criteria.json       # 动态评分标准
├── results/{skill_name}/{eval_id}/
│   ├── with_skill/{tc_id}.json     # 执行快照（有 skill）
│   └── without_skill/{tc_id}.json  # 执行快照（无 skill 基线）
├── reports/{skill_name}/{eval_id}/
│   ├── eval_data.json              # 结构化评测数据
│   └── report.html                 # HTML 可视化报告
└── aggregate/{profile_type}/{aggregate_id}/
    ├── aggregate_data.json
    └── aggregate_report.html
```

查看报告：

```bash
open storage/reports/<skill-name>/<eval-id>/report.html
```

---

## 配置 LLM

在项目根目录创建 `.env` 文件：

```dotenv
# Anthropic（推荐，用于 LLM API 执行路径和 LLM Judge 评判）
ANTHROPIC_API_KEY=sk-ant-...

# 或 OpenAI / 兼容端点
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-endpoint/v1

# 评测模型配置
EVAL_MODEL=claude-sonnet-4-5    # LLM API 路径执行测试用例的模型（Claude CLI 路径不受此控制）
JUDGE_MODEL=claude-opus-4-5     # 评分 Judge 使用的模型（需要 API Key 才能生效）

# 可选：运行时参数（默认值见 evaluator/config.py）
STORAGE_BASE_DIR=./storage
LAYER4_CASE_TIMEOUT=60
LAYER4_TOTAL_TIMEOUT=600
```

### L4 执行路径与模型选择

L4 动态评测按优先级选择执行路径：

| 优先级 | 路径 | 使用的模型 | 说明 |
|--------|------|-----------|------|
| 1 | Claude CLI | CLI 本地配置的默认模型 | `claude -p` 在隔离沙箱中执行，`EVAL_MODEL` 不生效 |
| 2 | Anthropic API | `EVAL_MODEL`（默认 `claude-opus-4-5`） | 需要 `ANTHROPIC_API_KEY` |
| 3 | OpenAI 兼容 API | `EVAL_MODEL` 或端点默认模型 | 需要 `OPENAI_API_KEY`，支持工具调用 |
| 4 | 本地入口点 | — | 直接运行 skill 的 `skill_entry.py` |

### 评判（Scoring）降级机制

评判阶段独立于执行路径，按优先级选择评判方式：

| 优先级 | 方式 | 条件 | 精度 |
|--------|------|------|------|
| 1 | 程序化检查 | `deterministic`/`workflow` profile | 高（结构化校验） |
| 2 | LLM Judge | 有 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY` | 高（语义评估） |
| 3 | 规则匹配 fallback | 无任何 API Key | 低（关键词匹配） |

> **注意**：如果没有配置任何 API Key，`JUDGE_MODEL` 不会生效，评判将降级为基于关键词的规则匹配，评分精度较低。建议至少配置一个 API Key。

---

## 最新更新

- **L5 报告新增独立「增量价值分析」section**：启用 `--with-baseline` 后，报告中新增独立的增量价值分析卡片，包含总览横幅（verdict 判定）、耗时 & Token 总计对比、逐用例对比明细表。
- **`regenerate-report` 命令增强**：分层检查 L1/L2/L4 数据完整性，缺失的层自动重新运行，不再依赖可能被覆盖的 eval_data.json 历史数据。
- **`eval_data.json` 新增 `effect_validation` 字段**：记录增量价值分析的结构化数据（delta_score、耗时对比、token 对比、逐用例明细），供外部工具消费。
- **without-skill 基线对比默认关闭**：可通过 `--with-baseline` 启用增量价值评分。
- **L4 `failure_reason` 修复**：成功执行的用例不再错误显示 `simulation_note` 作为失败原因。
- **L4 执行路径文档化**：新增执行路径优先级说明和评判降级机制文档。
- **MCP bridge 移除**：L4 已完全移除 MCP bridge 路径，统一使用 Claude CLI 沙箱隔离执行。
- L4 报告新增 `max_iterations=8` 失败用例专栏，并按执行路径分层展示。
- 文档统一补充安装方式与环境变量配置说明（README / USAGE / CLAUDE / SKILL）。

---

## 参考

- 完整 API 和参数说明：[SKILL.md](SKILL.md)
- 架构约定与禁止事项：[CLAUDE.md](CLAUDE.md)
- 详细使用说明：[USAGE.md](USAGE.md)
