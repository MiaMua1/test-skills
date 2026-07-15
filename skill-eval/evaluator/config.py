"""Unified configuration and dynamic scoring weights for the skill evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ScoreProfile:
    """Immutable scoring weight profile for one eval type."""

    layer1_max: float
    quality_max: float
    security_max: float
    robust_max: float
    correct_max: float
    delta_max: float

    def as_snapshot(self) -> dict[str, float]:
        """Serialise weights to a dict suitable for scoring_criteria.json."""
        return {
            "layer1_max": self.layer1_max,
            "quality_max": self.quality_max,
            "security_max": self.security_max,
            "robust_max": self.robust_max,
            "correct_max": self.correct_max,
            "delta_max": self.delta_max,
        }

    def total(self) -> float:
        """Sum of all weight components; must equal 100 for a valid profile."""
        return (
            self.layer1_max
            + self.quality_max
            + self.security_max
            + self.robust_max
            + self.correct_max
            + self.delta_max
        )


# fmt: off
SCORE_PROFILES: dict[str, ScoreProfile] = {
    "deterministic": ScoreProfile(layer1_max=15, quality_max=15, security_max=20,
                                   robust_max=8,  correct_max=12, delta_max=30),
    "generative":    ScoreProfile(layer1_max=15, quality_max=5,  security_max=15,
                                   robust_max=10, correct_max=55, delta_max=0),
    "workflow":      ScoreProfile(layer1_max=15, quality_max=10, security_max=15,
                                   robust_max=8,  correct_max=22, delta_max=30),
    "no_code":       ScoreProfile(layer1_max=20, quality_max=0,  security_max=10,
                                   robust_max=15, correct_max=55, delta_max=0),
}
# fmt: on

TYPE_TO_PROFILE: dict[str, str] = {
    "tool":      "deterministic",
    "analyzer":  "deterministic",
    "generator": "generative",
    "workflow":  "workflow",
}

GRADE_THRESHOLDS: list[tuple[float, str, str]] = [
    (90, "A", "PASSED"),
    (75, "B", "PASSED"),
    (60, "C", "PASSED"),
    (45, "D", "NEEDS_IMPROVEMENT"),
    (0,  "F", "FAILED"),
]

TYPE_INFERENCE_KEYWORDS: dict[str, list[str]] = {
    "workflow": [
        "workflow", "pipeline", "orchestrat",
        "multi-step", "chain", "sequence", "step",
        "工作流", "流程", "编排", "多步骤", "链式", "阶段",
    ],
    "generative": [
        "generat", "creat", "write", "produc", "draft",
        "composit", "synthes",
        "生成", "创作", "撰写", "产出", "起草", "输出文档",
    ],
    "analyzer": [
        "analyz", "extract", "classif", "detect",
        "parse", "evaluat", "assess",
        "分析", "提取", "分类", "检测", "解析", "评估", "审查",
    ],
}

EXCLUDED_DIRS: frozenset[str] = frozenset({
    "node_modules", ".venv", "venv", "env",
    "__pycache__", ".git", "dist", "build",
})

CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".rb", ".go",
})


class EvalSettings(BaseSettings):
    """Runtime configuration loaded from .env / environment variables."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    judge_model: str = "claude-opus-4-5"
    eval_model: str = "claude-opus-4-5"
    default_env_type: str = "local"
    storage_base_dir: str = "./storage"
    layer3_timeout: int = 120
    layer4_case_timeout: int = 60
    layer4_total_timeout: int = 600
    judge_passing_threshold: float = 0.7
    delta_normalize_offset: float = 0.5
    layer3_p0_count: int = 4
    layer3_p1_count: int = 4
    layer3_p2_count: int = 2

    # Tool availability pre-check for Layer 2 static analysis
    tool_availability_check: bool = True
    # Behaviour when tools are missing: "degrade" | "block" | "warn"
    tool_missing_policy: str = "degrade"


@lru_cache(maxsize=1)
def get_settings() -> EvalSettings:
    """Return a cached singleton EvalSettings instance."""
    return EvalSettings()


def get_score_profiles(with_baseline: bool = False) -> dict[str, ScoreProfile]:
    """Return all scoring profiles.

    If with_baseline is True, profiles include delta_max for incremental value scoring.
    If False, delta_max is set to 0 for profiles where it normally applies.
    """
    profiles = SCORE_PROFILES.copy()
    if not with_baseline:
        # When baseline is disabled, set delta_max to 0 (incremental value not calculated)
        for key in profiles:
            profile = profiles[key]
            profiles[key] = ScoreProfile(
                layer1_max=profile.layer1_max,
                quality_max=profile.quality_max,
                security_max=profile.security_max,
                robust_max=profile.robust_max,
                correct_max=profile.correct_max + profile.delta_max,  # redistribute delta to correctness
                delta_max=0.0,
            )
    return profiles


def calculate_grade(score: float) -> tuple[str, str]:
    """Return (grade_letter, verdict_string) for a total score."""
    for threshold, grade, verdict in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade, verdict
    return "F", "FAILED"
