"""Custom exception hierarchy for the skill evaluator."""

from __future__ import annotations


class EvaluationError(Exception):
    """Base class for all evaluator errors."""


class BlockedError(EvaluationError):
    """Evaluation blocked due to insufficient score or red-line trigger."""

    def __init__(self, layer: int, score: float, reason: str = "") -> None:
        self.layer = layer
        self.score = score
        self.reason = reason
        super().__init__(f"Blocked at layer {layer} (score={score}): {reason}")


class SkillInvalidError(EvaluationError):
    """Skill directory structure is invalid."""


class EnvError(EvaluationError):
    """Environment initialization failed."""


class JudgeError(EvaluationError):
    """LLM Judge call failed."""


class ScoreBindingError(EvaluationError):
    """eval_id mismatch or profile_weight_snapshot sum ≠ 100."""

    def __init__(self, reason: str, eval_id: str = "") -> None:
        self.eval_id = eval_id
        super().__init__(f"ScoreBindingError (eval_id={eval_id!r}): {reason}")


class AggregateError(EvaluationError):
    """Aggregate report generation failed."""
