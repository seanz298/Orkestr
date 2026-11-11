"""Deep Agents harness (LangGraph). Used for DeepSeek by default, but `model`
accepts any "provider:model" string LangChain understands.

NOT verified against a live install: written from the Deep Agents docs."""
from __future__ import annotations

import os
from typing import Any, Dict

from ..contract import Delivery
from ..gate import scrubbed_env


def _backend(cls, **kwargs):
    try:
        return cls(**kwargs)
    except TypeError:  # older/newer signature without virtual_mode
        kwargs.pop("virtual_mode", None)
        return cls(**kwargs)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")


def run(cfg: Dict[str, Any], req: Dict[str, Any]) -> Delivery:
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend, LocalShellBackend

    model = cfg.get("model", "")
    if not model or model.endswith(":"):
        raise RuntimeError("deepagents: model is empty; set it in .env / config.yaml")

    if req["read_only"]:
        # Files only, no shell. Any write is reverted by the orchestrator anyway.
        backend = _backend(FilesystemBackend, root_dir=req["worktree"], virtual_mode=True)
    else:
        # The agent's shell must not see API keys.
        backend = _backend(LocalShellBackend, root_dir=req["worktree"],
                           env=scrubbed_env(), virtual_mode=True)

    agent = create_deep_agent(model=model, system_prompt=req["system_prompt"], backend=backend)
    os.chdir(req["worktree"])
    hit_limit = False
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": req["prompt"]}]},
            config={"recursion_limit": req["max_turns"] * 2 + 10},
        )
        messages = result.get("messages", [])
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "GraphRecursionError":
            raise
        hit_limit, messages = True, []

    tin = tout = turns = 0
    for m in messages:
        usage = getattr(m, "usage_metadata", None)
        if usage:
            turns += 1
            tin += usage.get("input_tokens", 0)
            tout += usage.get("output_tokens", 0)
    cost = (tin * cfg.get("price_in", 0) + tout * cfg.get("price_out", 0)) / 1_000_000
    summary = _text(getattr(messages[-1], "content", "")) if messages else ""
    if hit_limit:
        # Let the quality gate judge whatever was done; cost is unknown here.
        summary = "[stopped: hit max turns]"
        turns = req["max_turns"]
    return Delivery(success=True, summary=summary, cost_usd=cost, turns=turns)
