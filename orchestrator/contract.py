"""The delivery contract. The orchestrator only ever looks at these objects;
what happens inside each agent harness is that harness's own business."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

# Task states
QUEUED = "QUEUED"
PLANNING = "PLANNING"
DEVELOPING = "DEVELOPING"
GATING = "GATING"
REVIEWING = "REVIEWING"
REPAIRING = "REPAIRING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
FAILED = "FAILED"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
INTERRUPTED = "INTERRUPTED"

IN_PROGRESS = {PLANNING, DEVELOPING, GATING, REVIEWING, REPAIRING}


class TaskSpec(BaseModel):
    """Input side of the contract: what the product owner asks for."""

    id: str = ""
    title: str
    description: str
    repo: str  # directory name under <workspace>/repos
    base_branch: str = "main"
    test_command: str  # the quality gate; exit code 0 == pass
    acceptance_criteria: List[str] = Field(default_factory=list)
    # Paths agents must not modify (e.g. acceptance tests written by the owner).
    protected_paths: List[str] = Field(default_factory=list)
    budget_usd: float = 2.0
    max_repair_rounds: int = 3
    use_moa: bool = False
    gate_timeout_s: int = 900


@dataclass
class AgentRequest:
    role: str  # planner | developer | reviewer | repairer
    prompt: str
    system_prompt: str
    worktree: Path
    read_only: bool
    max_turns: int = 60
    timeout_s: int = 1800


class Delivery(BaseModel):
    """Output side of the contract: what an agent hands back."""

    agent: str = ""
    role: str = ""
    success: bool = True
    summary: str = ""  # the agent's final message
    cost_usd: float = 0.0
    turns: int = 0
    error: Optional[str] = None


class GateReport(BaseModel):
    passed: bool
    reason: str = ""  # ok | no_changes | protected_paths | tests_failed | timeout
    changed_files: List[str] = Field(default_factory=list)
    protected_violations: List[str] = Field(default_factory=list)
    exit_code: Optional[int] = None
    output_tail: str = ""
