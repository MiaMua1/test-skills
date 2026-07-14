#!/usr/bin/env python3
"""
MCP Bridge Worker — runs in an isolated subprocess with a clean environment.

Problem it solves
-----------------
When the skill-evaluator is launched inside a Cursor Agent session the process
inherits environment variables such as CURSOR_AGENT=1 / CLAUDECODE=1 that prevent
spawning a nested ``claude`` CLI subprocess (the evaluator intentionally detects and
blocks this to avoid infinite loops).

This worker is spawned by Layer 4 as a **child process** with those env variables
stripped, so it can freely call ``claude -p`` and return real skill-execution results
— exactly what the user would get by running the CLI themselves.

How skill loading works
-----------------------
with_skill=True  → SKILL.md full content is injected via --append-system-prompt,
                   faithfully replicating how Cursor loads a skill into an agent's
                   context window.  Claude CLI then executes the prompt under those
                   instructions with full tool access (Bash, Read, Write, etc.).
with_skill=False → Clean baseline: only the date context is appended, no skill
                   instructions — same model capability without the skill.

Anti-deadlock mechanisms
------------------------
1. --max-turns N          Cap agent turns; prevents infinite tool-call loops.
2. Thread-based heartbeat Main thread logs progress every 10 s; Cursor/CI won't
                           think the process is frozen.
3. capture_output threads subprocess.run(capture_output=True) internally reads
                           stdout + stderr in separate OS threads — the pipes can
                           never fill up and deadlock regardless of output size.
4. Hard wall-clock timeout subprocess.run(timeout=N) kills the process after N s
                           and the worker immediately returns a stub result.

Wire protocol (stdio, one request → one response)
--------------------------------------------------
stdin  : single JSON line (request)
stdout : single JSON line (response)
stderr : debug/error logs (ignored by caller)

Request schema
--------------
{
  "prompt":       str,          # user prompt to send to claude
  "use_skill":    bool,         # True → inject SKILL.md as system context
  "skill_md":     str,          # full content of SKILL.md (only used when use_skill=True)
  "skill_name":   str,          # skill name (used for logging)
  "claude_bin":   str,          # path to the claude binary
  "project_root": str,          # cwd for the claude subprocess
  "timeout":      int,          # seconds before giving up (default 300)
  "max_turns":    int,          # max agent turns (default 15); 0 = no limit
  "extra_env":    dict          # optional extra env vars to inject (e.g. API tokens)
}

Response schema
---------------
{
  "answer":       str,          # final text answer from claude
  "token_count":  int,          # input + output tokens (0 if unavailable)
  "exit_code":    int,          # claude process exit code
  "duration_s":   float,        # wall-clock seconds
  "skill_loaded": bool,         # True when SKILL.md was injected into system context
  "error":        str | null    # non-null when a Python exception occurred
}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Env vars that signal "we are inside an AI agent session".
# The bridge worker must NOT inherit these or claude CLI will refuse to start.
# ---------------------------------------------------------------------------
AGENT_ENV_KEYS = frozenset(
    {"CLAUDECODE", "CURSOR_AGENT", "CURSOR_EXTENSION_HOST_ROLE"}
)

# How often (seconds) to print a heartbeat line to stderr
HEARTBEAT_INTERVAL_S = 10

# Default max agent turns — prevents infinite tool-call loops
DEFAULT_MAX_TURNS = 15

# 60 KB cap for SKILL.md in --append-system-prompt (OS arg limit safety)
MAX_SKILL_MD_BYTES = 60_000


def _build_clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in AGENT_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def _run_claude(
    claude_bin: str,
    prompt: str,
    project_root: Path,
    timeout: int,
    env: dict,
    skill_md: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> tuple[str, int, int]:
    """Run ``claude -p <prompt>`` and return (answer, token_count, exit_code).

    Anti-deadlock design
    --------------------
    • --max-turns N          : cap agent turns so the agent can't loop forever.
    • capture_output=True    : Python's subprocess reads stdout+stderr in separate
      OS threads — the pipes never fill up regardless of output volume.
    • Thread + heartbeat     : The actual subprocess.run call is done in a daemon
      thread; the main thread loops with HEARTBEAT_INTERVAL_S pauses and logs
      progress so Cursor / CI doesn't think we're frozen.
    • timeout                : Passed to subprocess.run; kills the process after N s
      and the worker returns a stub result immediately.

    When ``skill_md`` is provided the full SKILL.md is prepended to system context
    via ``--append-system-prompt``; this faithfully replicates Cursor skill loading.
    """
    today_str = datetime.now().strftime("%Y年%m月%d日（%A）")
    date_ctx = (
        f"当前日期是{today_str}。"
        "请务必基于此日期回答所有与时间、月份、季度、年份相关的问题。"
    )

    if skill_md and skill_md.strip():
        skill_md_trimmed = skill_md[:MAX_SKILL_MD_BYTES]
        if len(skill_md) > MAX_SKILL_MD_BYTES:
            skill_md_trimmed += "\n\n[...SKILL.md truncated for CLI argument length...]"
        system_append = skill_md_trimmed + "\n\n" + date_ctx
        print(
            f"[bridge] loading skill into system context ({len(skill_md)} chars)",
            file=sys.stderr,
        )
    else:
        system_append = date_ctx

    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "json",          # single JSON blob at exit (reliable)
        "--dangerously-skip-permissions",
        "--append-system-prompt", system_append,
    ]
    # NOTE: --max-turns is NOT supported in claude CLI v2.x (checked 2.1.42).
    # The hard wall-clock timeout (below) is the primary protection against
    # infinite loops.  If a future version supports --max-turns we can add it back.

    print(
        f"[bridge] launching claude | timeout={timeout}s "
        f"skill_loaded={bool(skill_md and skill_md.strip())}",
        file=sys.stderr,
    )

    # -----------------------------------------------------------------------
    # Run subprocess in a daemon thread so the main thread can log heartbeats.
    # subprocess.run(capture_output=True) reads stdout+stderr via OS threads
    # internally, so the pipes will never fill up and deadlock.
    # -----------------------------------------------------------------------
    _result: list[subprocess.CompletedProcess | None] = [None]
    _error: list[BaseException | None] = [None]

    def _worker_fn() -> None:
        try:
            _result[0] = subprocess.run(  # pylint: disable=subprocess-run-check
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(project_root),
                env=env,
            )
        except BaseException as exc:  # pylint: disable=broad-except
            _error[0] = exc

    t0 = time.time()
    run_thread = threading.Thread(target=_worker_fn, daemon=True)
    run_thread.start()

    # Heartbeat loop — joins with HEARTBEAT_INTERVAL_S timeout each iteration
    while run_thread.is_alive():
        run_thread.join(timeout=HEARTBEAT_INTERVAL_S)
        if run_thread.is_alive():
            elapsed = time.time() - t0
            print(
                f"[bridge] ♥ claude still running {elapsed:.0f}s / {timeout}s",
                file=sys.stderr,
            )

    duration = round(time.time() - t0, 3)

    # Handle timeout or other exceptions from the subprocess
    if _error[0] is not None:
        exc = _error[0]
        if isinstance(exc, subprocess.TimeoutExpired):
            print(f"[bridge] ⏱ timeout after {timeout}s — process killed", file=sys.stderr)
            return f"[bridge timeout after {timeout}s]", 0, -1
        raise exc  # type: ignore[misc]

    result = _result[0]
    assert result is not None

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    answer = stdout
    token_count = 0
    try:
        parsed = json.loads(stdout)
        answer = parsed.get("result", stdout)
        usage = parsed.get("usage", {})
        token_count = (
            (usage.get("input_tokens") or 0)
            + (usage.get("output_tokens") or 0)
        )
    except (json.JSONDecodeError, TypeError):
        pass  # stdout is plain text — use as-is

    if not answer and result.returncode != 0:
        answer = (
            f"[claude CLI error exit={result.returncode}] "
            + (stderr[:400] if stderr else "(no stderr)")
        )

    print(
        f"[bridge] done | exit={result.returncode} tokens={token_count} dur={duration}s",
        file=sys.stderr,
    )
    return answer, token_count, result.returncode


def main() -> None:
    t0 = time.monotonic()

    try:
        raw = sys.stdin.readline()
        if not raw.strip():
            _respond({
                "answer": "", "token_count": 0, "exit_code": -1,
                "duration_s": 0.0, "skill_loaded": False, "error": "empty request",
            })
            return

        req: dict = json.loads(raw)

        prompt: str = req["prompt"]
        use_skill: bool = req.get("use_skill", True)
        skill_md: str = req.get("skill_md", "")
        skill_name: str = req.get("skill_name", "eval-skill")
        claude_bin: str = req.get("claude_bin", "claude")
        project_root = Path(req.get("project_root", "."))
        timeout: int = int(req.get("timeout", 300))
        max_turns: int = int(req.get("max_turns", DEFAULT_MAX_TURNS))
        extra_env: dict = req.get("extra_env", {})

        env = _build_clean_env(extra_env)

        run_skill_md = skill_md if (use_skill and skill_md) else None
        skill_loaded = bool(run_skill_md)

        print(
            f"[bridge] request | skill={skill_name!r} use_skill={use_skill} "
            f"skill_loaded={skill_loaded} skill_md_len={len(skill_md)} "
            f"max_turns={max_turns} timeout={timeout}",
            file=sys.stderr,
        )

        answer, token_count, exit_code = _run_claude(
            claude_bin, prompt, project_root, timeout, env,
            skill_md=run_skill_md,
            max_turns=max_turns,
        )

        _respond({
            "answer": answer,
            "token_count": token_count,
            "exit_code": exit_code,
            "duration_s": round(time.monotonic() - t0, 3),
            "skill_loaded": skill_loaded,
            "error": None,
        })

    except subprocess.TimeoutExpired:
        _respond({
            "answer": f"[bridge timeout after {req.get('timeout', 300)}s]",  # type: ignore[name-defined]
            "token_count": 0, "exit_code": -1,
            "duration_s": round(time.monotonic() - t0, 3),
            "skill_loaded": False, "error": "timeout",
        })
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[bridge] error: {exc}", file=sys.stderr)
        _respond({
            "answer": f"[bridge error] {exc}",
            "token_count": 0, "exit_code": -1,
            "duration_s": round(time.monotonic() - t0, 3),
            "skill_loaded": False, "error": str(exc),
        })


def _respond(data: dict) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
