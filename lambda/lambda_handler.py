"""AWS Lambda entrypoint for the EG4 battery controller.

The CLI script (`guardrail.py`) does all the real work. This module is a
thin adapter that:
  1. Builds an argparse-compatible Namespace from the Lambda event payload.
  2. Runs the async `_run` coroutine and returns the exit code.

EventBridge Scheduler invokes this with an empty event (`{}`), which means
"normal scheduled run — honor EG4_DRY_RUN env var". For one-shot manual
invocations via `aws lambda invoke`, the event may include:
  {"discover": true}   → dump every setting (read-only)
  {"dry_run": true}    → force dry-run even if EG4_DRY_RUN=0
  {"apply": true}      → force write even if EG4_DRY_RUN=1
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import guardrail

# Lambda's default log level is WARNING; lift to INFO so our structured
# guardrail log line appears in CloudWatch.
logging.getLogger().setLevel(logging.INFO)


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event or {}
    args = SimpleNamespace(
        discover=bool(event.get("discover")),
        dry_run=bool(event.get("dry_run")),
        apply=bool(event.get("apply")),
    )
    exit_code = asyncio.run(guardrail._run(args))
    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "request_id": getattr(context, "aws_request_id", None),
    }
