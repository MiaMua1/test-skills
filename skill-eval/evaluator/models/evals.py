"""Data models for test cases, evals.json and scoring_criteria.json."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, field_validator


RobustnessCheckType = Literal[
    "no_exception", "not_empty", "timeout", "exit_code", "contains_field",
    "doc_coverage", "param_valid", "example_match", "logic_coherent",
]

Priority = Literal["P0", "P1", "P2"]
Source = Literal["auto", "manual"]
ScoreLevel = Literal["完全满足", "部分满足", "不满足"]


class RobustnessCheck(BaseModel):
    description: str
    check_type: RobustnessCheckType
    timeout_seconds: Optional[float] = None
    expected_exit_code: Optional[int] = None
    required_field: Optional[str] = None


class CorrectnessAssertion(BaseModel):
    criterion: str
    weight: float = 1.0

    @field_validator("criterion")
    @classmethod
    def criterion_min_length(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError(f"criterion must be ≥10 chars: {v!r}")
        return v

    @field_validator("weight")
    @classmethod
    def weight_in_range(cls, v: float) -> float:
        if not (0.1 <= v <= 3.0):
            raise ValueError(f"weight must be 0.1–3.0, got {v}")
        return v


class CoverageStats(BaseModel):
    p0_count: int
    p1_count: int
    p2_count: int


class TestCase(BaseModel):
    id: str
    priority: Priority
    source: Source = "auto"
    prompt: str
    expected_behavior: str
    context: dict = {}
    robustness_checks: list[RobustnessCheck] = []
    correctness_rubric: list[CorrectnessAssertion] = []
    baseline_prompt: Optional[str] = None


class EvalsConfig(BaseModel):
    """Persisted as storage/evals/{skill_name}/{eval_id}/evals.json."""

    skill_name: str
    eval_id: str
    skill_type: str
    eval_profile: str
    generated_at: datetime
    coverage: CoverageStats
    test_cases: list[TestCase]


# ── scoring_criteria.json models ──────────────────────────────────────────────

class RobustnessScoreRule(BaseModel):
    check_id: str
    description: str
    check_type: RobustnessCheckType
    pass_score: float = 1.0
    fail_score: float = 0.0
    weight: float = 1.0


class CorrectnessScoreRule(BaseModel):
    assertion_id: str
    criterion: str
    weight: float
    score_levels: dict[str, float] = {"完全满足": 1.0, "部分满足": 0.5, "不满足": 0.0}
    scoring_guidance: str


class DeltaScoringRule(BaseModel):
    delta_max: float
    formula: str
    guidance: str


class DynamicScoringCriteria(BaseModel):
    tc_id: str
    weight_snapshot: dict[str, float]
    robustness_scoring: list[RobustnessScoreRule]
    correctness_scoring: list[CorrectnessScoreRule]
    delta_scoring: Optional[DeltaScoringRule] = None


class ScoringCriteriaConfig(BaseModel):
    """Persisted as storage/evals/{skill_name}/{eval_id}/scoring_criteria.json."""

    eval_id: str
    skill_name: str
    eval_profile: str
    generated_at: datetime
    profile_weight_snapshot: dict[str, float]
    criteria_by_tc: list[DynamicScoringCriteria]

    def validate_weight_sum(self) -> None:
        """Raise ScoreBindingError if weights don't sum to 100."""
        from evaluator.models.exceptions import ScoreBindingError
        total = sum(self.profile_weight_snapshot.values())
        if abs(total - 100.0) > 0.01:
            raise ScoreBindingError(
                f"profile_weight_snapshot sum = {total} ≠ 100",
                eval_id=self.eval_id,
            )
