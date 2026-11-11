"""Claude through the official Claude Agent SDK (its native harness).

NOT verified against a live install: written from the Agent SDK docs."""
from __future__ import annotations

from typing import Any, Dict

from ..contract import Delivery

READ_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"]


def run(cfg: Dict[str, Any], req: Dict[str, Any]) -> Delivery:
    import anyio
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    opts: Dict[str, Any] = dict(
        system_prompt=req["system_prompt"], cwd=req["worktree"],
        max_turns=req["max_turns"],
    )
    if cfg.get("model"):
        opts["model"] = cfg["model"]
    if req["read_only"]:
        # allowed_tools only pre-approves; disallowed_tools is the real block.
        opts.update(allowed_tools=READ_TOOLS, disallowed_tools=WRITE_TOOLS,
                    permission_mode="default")
    else:
        opts.update(allowed_tools=READ_TOOLS + ["Write", "Edit", "Bash"],
                    permission_mode="acceptEdits")

    async def go():
        final = None
        async for message in query(prompt=req["prompt"], options=ClaudeAgentOptions(**opts)):
            if isinstance(message, ResultMessage):
                final = message
        return final

    final = anyio.run(go)
    if final is None:
        return Delivery(success=False, error="claude: no result message")
    is_error = bool(getattr(final, "is_error", False))
    return Delivery(
        success=not is_error,
        summary=getattr(final, "result", "") or "",
        cost_usd=float(getattr(final, "total_cost_usd", 0) or 0),
        turns=int(getattr(final, "num_turns", 0) or 0),
        error="claude reported an error" if is_error else None,
    )
