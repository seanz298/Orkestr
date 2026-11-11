"""Child-process entry point: reads a request on stdin, prints a Delivery."""
from __future__ import annotations

import json
import sys
import traceback

from ..contract import Delivery
from .base import MARKER


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        if job["type"] == "deepagents":
            from .deepagents_agent import run
        elif job["type"] == "codex":
            from .codex_agent import run
        elif job["type"] == "claude":
            from .claude_agent import run
        else:
            raise ValueError(f"unknown agent type {job['type']!r}")
        delivery = run(job["cfg"], job["request"])
    except Exception as exc:  # noqa: BLE001
        delivery = Delivery(success=False,
                            error=f"{exc!r}\n{traceback.format_exc()[-1500:]}")
    print(MARKER + delivery.model_dump_json(), flush=True)


if __name__ == "__main__":
    main()
