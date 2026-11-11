from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


class Roles(BaseModel):
    planner: Optional[str] = None
    developer: str
    reviewer: str
    repairer: str
    escalation: Optional[str] = None  # takes over the final repair round


class Limits(BaseModel):
    max_turns: int = 60
    agent_timeout_s: int = 1800


class MoAClient(BaseModel):
    name: str
    kind: str  # openai_compatible | anthropic
    model: str = ""
    base_url: str = ""
    key_env: str
    price_in: float = 0.0  # USD per million input tokens
    price_out: float = 0.0


class MoAConfig(BaseModel):
    proposers: List[MoAClient] = Field(default_factory=list)
    aggregator: Optional[MoAClient] = None


class Config(BaseModel):
    roles: Roles
    agents: Dict[str, Dict[str, Any]]
    limits: Limits = Field(default_factory=Limits)
    moa: MoAConfig = Field(default_factory=MoAConfig)


def _expand(value: Any) -> Any:
    """Replace ${VAR} with the environment value (empty string if unset)."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def config_path() -> Path:
    env = os.environ.get("ADT_CONFIG")
    if env:
        return Path(env)
    for candidate in (Path("/app/config.yaml"), Path("config.yaml")):
        if candidate.exists():
            return candidate
    return Path("config.yaml")


def load_config(path: Optional[Path] = None) -> Config:
    raw = yaml.safe_load((path or config_path()).read_text(encoding="utf-8"))
    return Config.model_validate(_expand(raw))


def workspace_root() -> Path:
    return Path(os.environ.get("ADT_WORKSPACE", "/workspace"))
