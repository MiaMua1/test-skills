#!/usr/bin/env python3
"""
Skill Evaluation Report Generator & Server

Reads structured evaluation data (eval_data.json or evaluation_results/),
injects it into report.html, and serves via a local HTTP server.

Usage:
  # Serve from a workspace directory (looks for eval_data.json)
  python generate_report.py /path/to/skill-eval-workspace

  # Or point directly to an eval_data.json file
  python generate_report.py --data /path/to/eval_data.json

  # Write static HTML instead of serving
  python generate_report.py /path/to/workspace --static /path/to/output.html

  # Specify port
  python generate_report.py /path/to/workspace --port 3118
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from utils import calculate_grade  # pylint: disable=wrong-import-position


TEMPLATE_PATH = Path(__file__).parent / "report.html"
PLACEHOLDER = "/*__SKILL_EVAL_DATA__*/"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_eval_data(workspace: Path) -> dict:
    """Load evaluation data from workspace directory.

    Looks for eval_data.json first, then tries to reconstruct from
    individual layer result files in evaluation_results/.
    """
    # 1. Prefer explicit eval_data.json
    for candidate in [
        workspace / "eval_data.json",
        workspace / "iteration-1" / "eval_data.json",
        workspace / "evaluation_results" / "eval_data.json",
    ]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    # 2. Try to reconstruct from evaluation_results/ directory
    results_dir = workspace / "evaluation_results"
    if results_dir.is_dir():
        return reconstruct_from_results(workspace, results_dir)

    raise FileNotFoundError(
        f"No eval_data.json found in {workspace}. "
        "Run an evaluation first or provide --data path."
    )


def reconstruct_from_results(skill_path: Path, results_dir: Path) -> dict:
    """Reconstruct eval data from individual layer result files."""
    data: dict = {
        "skill_name": skill_path.name,
        "version": "",
        "evaluation_date": datetime.now().isoformat(),
        "summary": {"total_score": 0, "max_score": 100, "grade": "F", "status": "completed"},
        "score_breakdown": [],
        "layers": {},
        "test_cases": [],
        "bugs": [],
        "recommendations": [],
        "key_findings": {"strengths": [], "issues": []},
        "effect_validation": None,
    }

    # Layer 1
    l1_file = results_dir / "layer1_results.json"
    if l1_file.exists():
        try:
            l1 = json.loads(l1_file.read_text())
            data["layers"]["layer1"] = l1
            data["score_breakdown"].append({
                "label": "快速过滤", "score": l1.get("score", 0), "max": 20, "layer": 1
            })
        except (json.JSONDecodeError, OSError):
            pass

    # Layer 2 - code quality
    cq_file = results_dir / "code_quality_results.json"
    sec_file = results_dir / "security_results.json"
    cq_data = {}
    sec_data = {}
    if cq_file.exists():
        try:
            cq_data = json.loads(cq_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    if sec_file.exists():
        try:
            sec_data = json.loads(sec_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if cq_data or sec_data:
        data["layers"]["layer2"] = {
            "code_quality": cq_data,
            "security": sec_data,
            "passed": not sec_data.get("critical_issues"),
        }
        if cq_data:
            data["score_breakdown"].append({
                "label": "代码质量", "score": cq_data.get("score", 0), "max": 20, "layer": 2
            })
        if sec_data:
            data["score_breakdown"].append({
                "label": "安全合规", "score": sec_data.get("score", 0), "max": 20, "layer": 2
            })

    # Summary
    summary_file = results_dir / "evaluation_summary.json"
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text())
            data["summary"].update({
                "total_score": summary.get("total_score", 0),
                "grade": summary.get("grade", "F"),
                "status": summary.get("status", "completed"),
                "blocking_reason": summary.get("blocking_reason"),
            })
            data["skill_name"] = Path(summary.get("skill_path", skill_path)).name
        except (json.JSONDecodeError, OSError):
            pass

    # Calculate total score from breakdown
    if data["summary"]["total_score"] == 0:
        data["summary"]["total_score"] = sum(
            item["score"] for item in data["score_breakdown"]
        )
        data["summary"]["grade"] = calculate_grade(data["summary"]["total_score"])

    return data


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(eval_data: dict) -> str:
    """Inject eval data into the report.html template."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(eval_data, ensure_ascii=False, indent=None)
    return template.replace(PLACEHOLDER, f"const SKILL_EVAL_DATA = {data_json};")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def _kill_port(port: int) -> None:
    """Kill any process listening on the specified port.

    Args:
        port: The port number to check and free.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for pid_str in result.stdout.strip().split("\n"):
            if pid_str.strip():
                try:
                    os.kill(int(pid_str.strip()), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
        if result.stdout.strip():
            time.sleep(0.5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


class ReportHandler(BaseHTTPRequestHandler):
    """HTTP request handler for serving the evaluation report."""

    def __init__(self, workspace: Path, data_path: Path | None, *args, **kwargs):
        """Initialize the report handler.

        Args:
            workspace: Path to the evaluation results workspace.
            data_path: Path to the eval data JSON file.
            *args: Additional positional arguments for BaseHTTPRequestHandler.
            **kwargs: Additional keyword arguments for BaseHTTPRequestHandler.
        """
        self.workspace = workspace
        self.data_path = data_path
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        """Handle GET requests to serve the report."""
        if self.path in ("/", "/index.html"):
            # Reload data on each request so the page auto-updates
            try:
                if self.data_path and self.data_path.exists():
                    eval_data = json.loads(self.data_path.read_text(encoding="utf-8"))
                else:
                    eval_data = load_eval_data(self.workspace)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                self._send_error(500, f"Failed to load eval data: {e}")
                return

            html = generate_html(eval_data)
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def _send_error(self, code: int, msg: str) -> None:
        """Send an HTTP error response.

        Args:
            code: HTTP error code.
            msg: Error message to display.
        """
        content = f"<html><body><h2>Error {code}</h2><pre>{msg}</pre></body></html>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, log_format: str, *args: object) -> None:
        """Suppress request logs.

        Args:
            log_format: Log format string (ignored).
            *args: Format arguments (ignored).
        """
        # Intentionally suppress logs - override parent method


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point for the report generator.

    Parses command-line arguments and either starts a local server
    or generates a static HTML report.
    """
    parser = argparse.ArgumentParser(
        description="Generate and serve skill evaluation report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "workspace", type=Path, nargs="?",
        help="Path to skill directory or evaluation workspace"
    )
    parser.add_argument(
        "--data", "-d", type=Path, default=None,
        help="Path to eval_data.json directly"
    )
    parser.add_argument(
        "--port", "-p", type=int, default=3118,
        help="Server port (default: 3118)"
    )
    parser.add_argument(
        "--static", "-s", type=Path, default=None,
        help="Write standalone HTML to this path instead of starting server"
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't open browser automatically"
    )
    args = parser.parse_args()

    # Determine data source
    data_path: Path | None = None
    workspace: Path = Path(".")

    if args.data:
        data_path = args.data.resolve()
        if not data_path.exists():
            print(f"Error: data file not found: {data_path}", file=sys.stderr)
            sys.exit(1)
        workspace = data_path.parent
        try:
            eval_data = json.loads(data_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error loading data: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.workspace:
        workspace = args.workspace.resolve()
        if not workspace.exists():
            print(f"Error: path not found: {workspace}", file=sys.stderr)
            sys.exit(1)
        try:
            eval_data = load_eval_data(workspace)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    skill_name = eval_data.get("skill_name", workspace.name)

    # Static mode
    if args.static:
        # Guard: never overwrite the template itself
        if args.static.resolve() == TEMPLATE_PATH.resolve():
            print(
                f"Error: output path cannot be the template file itself ({TEMPLATE_PATH}).\n"
                "Please specify a different output path.",
                file=sys.stderr,
            )
            sys.exit(1)
        html = generate_html(eval_data)
        args.static.parent.mkdir(parents=True, exist_ok=True)
        args.static.write_text(html, encoding="utf-8")
        print(f"\n  ✅ Report written to: {args.static}\n")
        sys.exit(0)

    # Server mode
    port = args.port
    _kill_port(port)
    handler = partial(ReportHandler, workspace, data_path)

    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]

    url = f"http://localhost:{port}"
    score = eval_data.get("summary", {}).get("total_score", "?")
    grade = eval_data.get("summary", {}).get("grade", "?")

    print("\n  Skill Eval Report Viewer")
    print(f"  {'─' * 40}")
    print(f"  Skill:   {skill_name}")
    print(f"  Score:   {score}/100  (Grade {grade})")
    print(f"  URL:     {url}")
    print(f"  Data:    {data_path or workspace}")
    print("\n  Page auto-reloads on refresh.")
    print("  Press Ctrl+C to stop.\n")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
