from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import Any, Dict, Protocol

from ..contract import AgentRequest, Delivery
from ..gate import scrubbed_env

MARKER = "@@DELIVERY@@"


class Agent(Protocol):
    def run(self, req: AgentRequest) -> Delivery: ...


class SubprocessAgent:
    """Runs a real harness in a child process.

    Why a child process: (1) a hard timeout that actually kills the agent and
    everything it spawned, (2) each agent only sees its own API key."""

    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name, self.cfg = name, cfg

    def run(self, req: AgentRequest) -> Delivery:
        payload = json.dumps({
            "type": self.cfg["type"], "cfg": self.cfg,
            "request": {
                "role": req.role, "prompt": req.prompt,
                "system_prompt": req.system_prompt, "worktree": str(req.worktree),
                "read_only": req.read_only, "max_turns": req.max_turns,
                "timeout_s": req.timeout_s,
            },
        })
        env = scrubbed_env(keep=self.cfg.get("env_keys", []))
        proc = subprocess.Popen(
            [sys.executable, "-m", "orchestrator.agents.runner"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
        try:
            out, err = proc.communicate(payload, timeout=req.timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return Delivery(success=False, error=f"timeout after {req.timeout_s}s")
        for line in reversed(out.splitlines()):
            if line.startswith(MARKER):
                return Delivery.model_validate_json(line[len(MARKER):])
        return Delivery(success=False,
                        error=f"harness exited {proc.returncode} without a delivery: {err[-1500:]}")
