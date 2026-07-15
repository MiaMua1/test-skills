"""Data models for test case execution results and reports."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


RunMode = Literal["with_skill", "without_skill"]
RunStatus = Literal["success", "failed", "timeout"]


class RunInput(BaseModel):
    prompt: str
    skill_used: Optional[str] = None
    skill_version: Optional[str] = None
    context: dict = {}


class ToolCallRecord(BaseModel):
    tool_name: str
    tool_input: dict
    tool_output: str


class RunOutput(BaseModel):
    raw_response: str
    tool_calls: list[ToolCallRecord] = []
    final_answer: str = ""


class RobustnessCheckResult(BaseModel):
    check_id: str
    check_type: str
    passed: bool
    detail: str = ""


class CorrectnessCheckResult(BaseModel):
    assertion_id: str
    criterion: str
    score: float
    level: str
    reasoning: str


class TestCaseResult(BaseModel):
    """Persisted as storage/results/{skill_name}/{eval_id}/{run_mode}/{tc_id}.json."""

    tc_id: str
    eval_id: str
    skill_name: str
    run_mode: RunMode
    executed_at: datetime
    duration_seconds: float
    status: RunStatus
    input: RunInput
    output: RunOutput
    robustness_results: list[RobustnessCheckResult] = []
    correctness_results: list[CorrectnessCheckResult] = []
    scores: dict[str, float] = {}
    tool_availability: Optional[dict] = None


class ScoreBreakdownItem(BaseModel):
    """One row in score_breakdown inside eval_data.json."""

    label: str
    score: float
    max_score: float
    layer: int
    source: str
    source_tc_ids: list[str] = []


class DimensionStat(BaseModel):
    label: str
    max_score: float
    avg_score: float
    min_score: float
    max_achieved: float
    layer: int


class AggregateData(BaseModel):
    """Persisted as storage/aggregate/{profile_type}/{aggregate_id}/aggregate_data.json."""

    aggregate_id: str
    aggregate_type: str
    aggregate_type_desc: str
    generated_at: datetime
    profile_weight_snapshot: dict[str, float]
    included_evals: list[dict]
    aggregate_stats: dict
    dimension_stats: list[DimensionStat]
    skill_comparison: list[dict]
    common_issues: list[dict]
    recommendations: list[str]
