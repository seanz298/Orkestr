"""Mixture-of-Agents planning, started on demand per task.

Several models propose a plan independently (plain chat calls, no tools),
then an aggregator model merges them into one plan for the developer agent."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Tuple

from .config import MoAClient, MoAConfig

ChatFn = Callable[[MoAClient, str, str], Tuple[str, float]]

PROPOSER_SYSTEM = (
    "You are a senior software engineer. Propose a concrete implementation plan "
    "for the task: files to touch, approach, edge cases, and how to verify. "
    "Be specific and concise. Do not write the full code."
)
AGGREGATOR_SYSTEM = (
    "You are the tech lead. Several engineers proposed plans independently. "
    "Merge them into ONE final plan: keep what is sound, resolve conflicts, "
    "drop anything speculative. Output only the final plan."
)


def chat(client: MoAClient, system: str, user: str) -> Tuple[str, float]:
    """Returns (text, cost_usd). Real network call."""
    import httpx

    key = os.environ.get(client.key_env, "")
    if not key:
        raise RuntimeError(f"{client.key_env} is not set")
    if client.kind == "anthropic":
        r = httpx.post(
            (client.base_url or "https://api.anthropic.com") + "/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": client.model, "max_tokens": 4096, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=300,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        usage = data.get("usage", {})
        tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    elif client.kind == "openai_compatible":
        r = httpx.post(
            client.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": client.model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}]},
            timeout=300,
        )
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    else:
        raise ValueError(f"unknown MoA client kind: {client.kind}")
    cost = (tin * client.price_in + tout * client.price_out) / 1_000_000
    return text, cost


def run_moa(cfg: MoAConfig, task_text: str, chat_fn: ChatFn = chat) -> Tuple[str, float, List[str]]:
    """Returns (plan, cost_usd, notes). Tolerates individual proposer failures."""
    proposers = [p for p in cfg.proposers if p.model]
    if not proposers or cfg.aggregator is None or not cfg.aggregator.model:
        raise RuntimeError("MoA needs at least one proposer and an aggregator with a model set")

    def one(client: MoAClient):
        try:
            text, cost = chat_fn(client, PROPOSER_SYSTEM, task_text)
            return client.name, text, cost, None
        except Exception as exc:  # noqa: BLE001 - recorded, not fatal
            return client.name, "", 0.0, repr(exc)

    with ThreadPoolExecutor(max_workers=len(proposers)) as pool:
        results = list(pool.map(one, proposers))

    total = sum(r[2] for r in results)
    notes = [f"{name}: {'ok' if not err else 'FAILED ' + err}" for name, _, _, err in results]
    good = [(name, text) for name, text, _, err in results if not err and text.strip()]
    if not good:
        raise RuntimeError("all MoA proposers failed: " + "; ".join(notes))

    merged_input = task_text + "\n\n" + "\n\n".join(
        f"=== Proposal {i + 1} ===\n{text}" for i, (_, text) in enumerate(good)
    )
    plan, cost = chat_fn(cfg.aggregator, AGGREGATOR_SYSTEM, merged_input)
    return plan, total + cost, notes
