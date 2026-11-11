"""End-to-end run of the whole pipeline with scripted fake agents.

Needs no API key and no network. Proves the orchestrator, git isolation,
quality gate, repair loop, read-only enforcement and approval merge work
on this machine. It does NOT exercise the real Codex/DeepSeek/Claude harnesses."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict

from . import contract as C
from .config import Config
from .contract import AgentRequest, Delivery, TaskSpec
from .pipeline import Pipeline
from .store import Store
from .workspace import Workspace, git

TEST_CMD = f"{sys.executable} -m unittest discover -s tests/acceptance -q"
APPROVE = 'Looks right.\n{"verdict": "approve", "comments": []}'


class FnAgent:
    def __init__(self, fn: Callable[[AgentRequest], Delivery]):
        self.fn = fn

    def run(self, req: AgentRequest) -> Delivery:
        return self.fn(req)


def make_demo_repo(ws: Workspace, name: str = "demo") -> Path:
    repo = ws.repos / name
    (repo / "tests" / "acceptance").mkdir(parents=True)
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests" / "acceptance" / "test_calc.py").write_text(
        "import sys, unittest\nsys.path.insert(0, '.')\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
    )
    git(["init", "-b", "main"], repo)
    git(["add", "-A"], repo)
    git(["commit", "-m", "initial (buggy)"], repo)
    return repo


def demo_spec(**over) -> TaskSpec:
    base = dict(
        title="Fix add()", description="calc.add subtracts instead of adding.",
        repo="demo", test_command=TEST_CMD,
        acceptance_criteria=["add(2, 3) == 5"],
        protected_paths=["tests/acceptance/*"], budget_usd=1.0, max_repair_rounds=2,
    )
    base.update(over)
    return TaskSpec(**base)


def demo_config() -> Config:
    return Config.model_validate({
        "roles": {"planner": "pm", "developer": "dev", "reviewer": "rev",
                  "repairer": "dev2", "escalation": None},
        "agents": {},
    })


def write_calc(body: str, cost: float = 0.01) -> Callable[[AgentRequest], Delivery]:
    def fn(req: AgentRequest) -> Delivery:
        (req.worktree / "calc.py").write_text(f"def add(a, b):\n    return {body}\n")
        return Delivery(summary=f"set add to {body}", cost_usd=cost, turns=1)
    return fn


def default_agents() -> Dict[str, FnAgent]:
    def reviewer(req: AgentRequest) -> Delivery:
        # A misbehaving reviewer: tries to write. The orchestrator must revert it.
        (req.worktree / "reviewer_was_here.txt").write_text("oops")
        return Delivery(summary=APPROVE, cost_usd=0.01, turns=1)

    return {
        "pm": FnAgent(lambda r: Delivery(summary="Plan: change '-' to '+'.", cost_usd=0.01)),
        "dev": FnAgent(write_calc("a + b + 1")),   # first attempt is wrong
        "dev2": FnAgent(write_calc("a + b")),      # repair gets it right
        "rev": FnAgent(reviewer),
    }


def run_selftest(root: Path) -> None:
    ws = Workspace(root)
    store = Store(root / "state" / "orchestrator.db")
    repo = make_demo_repo(ws)
    pipe = Pipeline(demo_config(), store, ws, default_agents())
    tid = store.add_task(demo_spec())
    state = pipe.run_task(tid)

    kinds = [e["kind"] for e in store.events(tid)]
    assert state == C.AWAITING_APPROVAL, f"expected AWAITING_APPROVAL, got {state}: {store.get(tid)['note']}"
    assert kinds.count("gate") == 2, "expected one failed gate and one passed gate"
    assert "readonly_violation" in kinds, "reviewer write was not caught"
    assert not (ws.worktree(store.spec(tid)) / "reviewer_was_here.txt").exists()
    assert "a - b" in (repo / "calc.py").read_text(), "base branch changed before approval"

    ws.merge(store.spec(tid))
    assert "a + b\n" in (repo / "calc.py").read_text(), "merge did not land the fix"
