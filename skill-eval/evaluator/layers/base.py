"""Abstract base class for all evaluation layers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import structlog

from evaluator.models.skill import SkillInfo

logger = structlog.get_logger()


class BaseLayer(ABC):
    """Base class that all evaluation layers must inherit."""

    layer_number: int
    layer_name: str

    def __init__(self, skill_info: SkillInfo, storage_base: Path) -> None:
        self.skill_info = skill_info
        self.storage_base = storage_base
        self.log = logger.bind(layer=self.layer_name, skill=skill_info.metadata.name)

    @abstractmethod
    async def run(self, eval_id: str) -> dict:
        """Execute this layer and return a result dict.

        Args:
            eval_id: The shared evaluation identifier for this run.

        Returns:
            Layer-specific result dictionary.
        """

    def _skill_storage(self, sub: str, eval_id: str) -> Path:
        """Compute storage path: storage/{sub}/{skill_name}/{eval_id}/"""
        p = self.storage_base / sub / self.skill_info.metadata.name / eval_id
        p.mkdir(parents=True, exist_ok=True)
        return p
