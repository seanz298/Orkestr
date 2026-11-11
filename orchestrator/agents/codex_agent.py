"""Codex keeps its own native harness; we drive it through `codex exec`.

NOT verified against a live install: written from the Codex non-interactive docs."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

from ..contract import Delivery


def _login(binary: str, env: Dict[str, str]) -> None:
    key = env.get("OPENAI_API_KEY")
    if not key or (Path.home() / ".codex" / "auth.json").exists():
        return
    p = subprocess.run([binary, "login", "--with-api-key"], input=key,
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        subprocess.run([binary, "login", "--api-key", key],
                       capture_output=True, text=True, env=env)


def run(cfg: Dict[str, Any], req: Dict[str, Any]) -> Delivery:
    binary = cfg.get("binary", "codex")
    env = os.environ.copy()
    if env.get("OPENAI_API_KEY"):
        env.setdefault("CODEX_API_KEY", env["OPENAI_API_KEY"])
    _login(binary, env)

    sandbox = (cfg.get("sandbox_read", "read-only") if req["read_only"]
               else cfg.get("sandbox_write", "workspace-write"))
    last = tempfile.NamedTemporaryFile(suffix=".txt", delete=False).name
    cmd = [binary, "exec", "--json", "--skip-git-repo-check",
           "--sandbox", sandbox, "-C", req["worktree"], "-o", last]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    cmd.append(req["system_prompt"] + "\n\n" + req["prompt"])

    p = subprocess.run(cmd, capture_output=True, text=True, env=env)

    tin = tout = turns = 0
    fallback = ""
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        usage = ev.get("usage")
        if isinstance(usage, dict):
            turns += 1
            tin += usage.get("input_tokens", 0)
            tout += usage.get("output_tokens", 0)
        item = ev.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            fallback = item.get("text", fallback)

    summary = ""
    try:
        summary = Path(last).read_text(encoding="utf-8")
        os.unlink(last)
    except OSError:
        pass
    cost = (tin * cfg.get("price_in", 0) + tout * cfg.get("price_out", 0)) / 1_000_000
    ok = p.returncode == 0
    return Delivery(
        success=ok, summary=summary or fallback, cost_usd=cost, turns=turns,
        error=None if ok else f"codex exited {p.returncode}: {p.stderr[-1500:]}",
    )
