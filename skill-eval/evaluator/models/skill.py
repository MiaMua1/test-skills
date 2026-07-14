"""Data models for skill metadata and info."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator


class SkillType(str, Enum):
    TOOL = "tool"
    ANALYZER = "analyzer"
    GENERATOR = "generator"
    WORKFLOW = "workflow"


class EvalProfile(str, Enum):
    DETERMINISTIC = "deterministic"
    GENERATIVE = "generative"
    WORKFLOW = "workflow"
    NO_CODE = "no_code"


class SkillMetadata(BaseModel):
    """Validated skill metadata from skill.json or SKILL.md frontmatter."""

    name: str
    version: Optional[str] = None
    type: Optional[SkillType] = None
    description: str
    author: Optional[str] = None
    tags: list[str] = []
    dependencies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_be_kebab_case(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z0-9-]+$", v):
            raise ValueError(f"name must be kebab-case: {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def version_must_be_semver(cls, v: Optional[str]) -> Optional[str]:
        import re
        if v is not None and not re.match(r"^\d+\.\d+\.\d+", v):
            raise ValueError(f"version must be semver: {v!r}")
        return v

    @field_validator("description")
    @classmethod
    def description_min_length(cls, v: str) -> str:
        if len(v) < 20:
            raise ValueError(f"description must be ≥20 chars, got {len(v)}")
        return v


class SkillInfo(BaseModel):
    """Complete skill information used throughout the evaluation pipeline."""

    metadata: SkillMetadata
    skill_path: Path
    has_code: bool
    eval_profile: EvalProfile
    type_inferred: bool = False

    model_config = {"arbitrary_types_allowed": True}
