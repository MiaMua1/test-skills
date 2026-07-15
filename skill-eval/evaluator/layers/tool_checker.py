"""Tool availability checker for Layer 2 static analysis tools.

Validates that pylint, radon, bandit, and pip-audit are installed and
runnable before Layer 2 execution begins.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()

# Tools checked by this component
CHECKED_TOOLS: tuple[str, ...] = ("pylint", "radon", "bandit", "pip-audit")


class ToolStatus(str, Enum):
    """Individual tool availability status."""

    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"
    RUN_ERROR = "run_error"


@dataclass
class ToolAvailabilityResult:
    """Result of checking all Layer 2 tools."""

    tools: dict[str, ToolStatus] = field(default_factory=dict)
    all_available: bool = False
    none_available: bool = False
    available_count: int = 0
    total_count: int = 0

    def as_dict(self) -> dict:
        """Return a serialisable dict representation."""
        return {
            "tools": {name: status.value for name, status in self.tools.items()},
            "all_available": self.all_available,
            "none_available": self.none_available,
            "available_count": self.available_count,
            "total_count": self.total_count,
        }


class ToolAvailabilityChecker:
    """Check that all Layer 2 static analysis tools are installed and runnable.

    Uses shutil.which() to detect binary presence, then runs ``--version``
    to confirm the tool is actually executable.
    """

    def __init__(self, tool_names: Optional[tuple[str, ...]] = None) -> None:
        self.tool_names = tool_names or CHECKED_TOOLS

    async def check_all(self) -> ToolAvailabilityResult:
        """Check all configured tools concurrently.

        Returns:
            ToolAvailabilityResult with per-tool status and aggregate flags.
        """
        tasks = {name: self._check_one(name) for name in self.tool_names}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        tools: dict[str, ToolStatus] = {}
        for name, result in zip(self.tool_names, results):
            if isinstance(result, Exception):
                logger.warning("tool_checker.check_error",
                               tool=name, error=str(result))
                tools[name] = ToolStatus.RUN_ERROR
            else:
                tools[name] = result

        available_count = sum(
            1 for s in tools.values() if s == ToolStatus.AVAILABLE
        )
        total = len(tools)

        return ToolAvailabilityResult(
            tools=tools,
            all_available=(available_count == total),
            none_available=(available_count == 0),
            available_count=available_count,
            total_count=total,
        )

    async def _check_one(self, name: str) -> ToolStatus:
        """Check a single tool: binary presence + ``--version`` smoke test."""
        # 1. Check if the binary is on PATH
        if shutil.which(name) is None:
            logger.info("tool_checker.not_installed", tool=name)
            return ToolStatus.NOT_INSTALLED

        # 2. Run --version to confirm it is executable
        try:
            proc = await asyncio.create_subprocess_exec(
                name, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )
            if proc.returncode == 0:
                version = (stdout or stderr).decode(errors="replace").strip()
                logger.info("tool_checker.available",
                            tool=name, version=version[:80])
                return ToolStatus.AVAILABLE
            else:
                logger.warning("tool_checker.run_error",
                               tool=name, returncode=proc.returncode)
                return ToolStatus.RUN_ERROR
        except asyncio.TimeoutError:
            logger.warning("tool_checker.timeout", tool=name)
            return ToolStatus.RUN_ERROR
        except OSError as exc:
            logger.warning("tool_checker.os_error",
                           tool=name, error=str(exc))
            return ToolStatus.RUN_ERROR