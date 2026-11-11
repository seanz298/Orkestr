from __future__ import annotations

from typing import Dict

from ..config import Config
from .base import Agent, SubprocessAgent

KNOWN_TYPES = {"deepagents", "codex", "claude"}


def build_agents(cfg: Config) -> Dict[str, Agent]:
    agents: Dict[str, Agent] = {}
    for name, agent_cfg in cfg.agents.items():
        if agent_cfg.get("type") not in KNOWN_TYPES:
            raise ValueError(f"agent '{name}': unknown type {agent_cfg.get('type')!r}")
        agents[name] = SubprocessAgent(name, agent_cfg)
    return agents
