# CLAUDE.md

## 项目简介
Skills 质量评测框架 v1.0。对 Claude 工具插件（Skill）进行五层 + 聚合自动化质量评测，输出 HTML 报告和聚合报告。框架本身封装为单个 Skill tool，由 `skill_entry.py` 作为统一入口。

## 技术栈
- Python 3.11+，全程 asyncio 异步
- Pydantic v2 + pydantic-settings
- anthropic SDK AsyncAnthropic
- pylint / radon / bandit / pip-audit（可选，无法执行时降级为 LLM 推理）
- jinja2 / structlog / click / pyyaml / gitpython

## 目录职责
```
skill.json + SKILL.md + skill_entry.py  → Skill 包装层（单个 tool）
evaluator/config.py      → 所有配置和动态权重，get_config() 单例
evaluator/pipeline.py    → 主流程，串联各层
evaluator/cli.py         → CLI 入口（click）
evaluator/models/        → Pydantic 数据模型，层间共享
evaluator/layers/        → 各评测层，继承 BaseLayer
evaluator/environments/  → 环境提供者，继承 IEnvironmentProvider
evaluator/judge/         → LLM Judge，独立模块
evaluator/reporters/     → HTML 报告生成（单次 + 聚合）
storage/                 → 评测产物，已加入 .gitignore
```

## 核心约定

### 禁止事项
- 禁止 `print()` / `logging`，统一用 `structlog`
- 禁止裸 dict 层间传数据，用 Pydantic model
- 禁止 `Any` 类型（仅在必要的 JSON 反序列化边界使用）
- 禁止硬编码分数线/权重/超时，从 `config.py` 读取
- 禁止同步阻塞 IO，用 `asyncio.create_subprocess_exec`
- 禁止在 layer 内直接调用 Anthropic API，通过 `judge/llm_judge.py` 模块
- 禁止 `generator / no_code` 类计算 delta，`delta_score` 固定为 0
- 禁止在 `score_breakdown` 中手填 `max_score`，必须从 `scoring_criteria.json` 的 `profile_weight_snapshot` 读取

### 日志规范
```python
import structlog
logger = structlog.get_logger()
logger.info("layer3.criteria_saved", eval_id=eval_id, tc_count=n)
logger.error("layer5.binding_error", reason=reason, eval_id=eval_id)
```

### 异步规范
- 所有 IO 操作 `async/await`
- 多个独立 IO 并发用 `asyncio.gather()`
- 超时用 `asyncio.wait_for(coro, timeout=N)`
- 每条用例执行完立即写入快照，不批量写

### 异常体系
```
EvaluationError（基类）
├── BlockedError          layer, score
├── SkillInvalidError
├── EnvError
├── JudgeError
├── ScoreBindingError     eval_id 不一致 / max_score 合计 ≠ 100
└── AggregateError        聚合报告生成失败
```

## 评测 Profile 与动态权重

`skill.json` 的 `type` 字段 + 是否有代码文件 决定 `eval_profile`：

```
有代码文件：
  tool/analyzer → deterministic
  generator     → generative
  workflow      → workflow
无代码文件：自动切换为 no_code（忽略 type 字段）
```

各 profile 满分上限从 `config.SCORE_PROFILES` 读取：
```
profile.layer1_max / quality_max / security_max
profile.robust_max / correct_max / delta_max
```

| Profile | layer1 | quality | security | robust | correct | delta | 合计 |
|---------|--------|---------|----------|--------|---------|-------|------|
| deterministic | 15 | 15 | 20 | 8 | 12 | 30 | 100 |
| generative | 15 | 5 | 15 | 10 | 55 | 0 | 100 |
| workflow | 15 | 10 | 15 | 8 | 22 | 30 | 100 |
| no_code | 20 | 0 | 10 | 15 | 55 | 0 | 100 |

## 存储规范

```
storage/evals/{skill_name}/{eval_id}/evals.json
storage/evals/{skill_name}/{eval_id}/scoring_criteria.json
storage/results/{skill_name}/{eval_id}/with_skill/{tc_id}.json
storage/results/{skill_name}/{eval_id}/without_skill/{tc_id}.json
storage/reports/{skill_name}/{eval_id}/eval_data.json
storage/reports/{skill_name}/{eval_id}/report.html
storage/aggregate/{profile_type}/{aggregate_id}/aggregate_data.json
storage/aggregate/{profile_type}/{aggregate_id}/aggregate_report.html

eval_id 格式：{skill_name}-{YYYYMMDD-HHMMSS}-{uuid4前8位}
aggregate_id 格式：{profile_type}-aggregate-{YYYYMMDD-HHMMSS}-{uuid4前8位}
```

## 第三层约束
- `evals.json` 与 `scoring_criteria.json` 必须同一批次生成
- 两个文件共享同一 `eval_id`，写入同一目录
- `scoring_criteria` 的 `profile_weight_snapshot` 从 `ScoreProfile` 实时读取并冻结
- `correctness_scoring` 的 `scoring_guidance` 由 LLM 针对每个用例动态生成
- `evals.json` 写完后立即生成 `scoring_criteria.json`，不可分批

## 第四层约束
- 执行前读取 `scoring_criteria.json`，校验 `eval_id` 与当前一致
- 每条用例执行完立即写入 `{tc_id}.json`，不等全部完成再批量写
- `with_skill` 和 `without_skill` 各自独立存档
- 执行失败时 `output.raw_response` 记录异常信息，`status = "failed"`，不中断批跑
- `scores` 字段记录该用例各维度原始得分（0-1 区间）

## 第五层约束
- 生成前必须执行三项校验：
  ① `eval_id` 一致性（`scoring_criteria / results / report` 三者一致）
  ② `profile_weight_snapshot` 各项之和 = 100，不等则抛 `ScoreBindingError`
  ③ `score_breakdown` 每项 `max_score` 从快照读取，禁止手填
- `report.html` 必须输出到 `storage/reports/{skill_name}/{eval_id}/report.html`
- 严禁覆盖 `evaluator/reporters/templates/report.html.j2` 模板文件
- L4 用例详情需保留"执行路径分层"视图；对 `max_iterations=8` 的执行失败用例需单独分组展示，便于排障
- `regenerate-report` 重建 `layer_results` 时，分层检查 L1/L2/L4 数据完整性：L1/L2 数据缺失或为空时自动重新运行静态检查，L4 从 snapshot 文件恢复，不再盲信 `eval_data.json` 中可能被覆盖的历史数据
- `eval_data.json` 的 `effect_validation` 字段记录增量价值分析的结构化数据（`delta_score`、`total_with/without_duration_s`、`total_with/without_tokens`、`per_case` 逐用例明细），供 HTML 报告和外部工具消费
- L5 HTML 报告在启用 `--with-baseline` 时，在 L4 详情和「发现 & 建议」之间渲染独立的「增量价值分析」section，包含总览横幅、耗时 & Token 总计对比、逐用例对比明细表

## 聚合层约束
- 触发前校验所有 `eval_id` 的 `profile_weight_snapshot` 完全一致
- 不一致则抛 `AggregateError`，说明哪个 `eval_id` 权重不同
- `aggregate_report.html` 顶部必须显示 `aggregate_type` 和权重说明
- `dimension_stats` 的 `max_score` 统一从 `profile_weight_snapshot` 读取

## 分数计算公式
```
layer4_score：
robust_score = avg_robust(0-1) × robust_max
correct_score = avg_correct(0-1) × correct_max
delta_raw = with_correct - without_correct  # -1 ~ +1
delta_norm = max(0, delta_raw + 0.5)        # 0 ~ 1
delta_score = delta_norm × delta_max
（generator / no_code：delta_score = 0，跳过 without-skill）
```

## 环境变量约定（与 config.py 保持一致）

核心 LLM 配置：
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `JUDGE_MODEL`
- `EVAL_MODEL`

运行时配置（可选）：
- `STORAGE_BASE_DIR`（默认 `./storage`）
- `LAYER3_TIMEOUT`（默认 120）
- `LAYER4_CASE_TIMEOUT`（默认 60）
- `LAYER4_TOTAL_TIMEOUT`（默认 600）
- `JUDGE_PASSING_THRESHOLD`（默认 0.7）
- `DELTA_NORMALIZE_OFFSET`（默认 0.5）
