"""Task lifecycle: plan -> develop -> gate -> review -> repair loop -> human approval."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import contract as C
from .agents.base import Agent
from .config import Config
from .contract import AgentRequest, Delivery, GateReport, TaskSpec
from .gate import run_gate
from .memory import (MAX_HINTS, POSTMORTEM_SYSTEM, Memory, MemoryContext, NullMemory,
                     outcome_from_events, parse_attribution)
from .moa import ChatFn, chat, run_moa
from .store import Store
from .workspace import Workspace

PLAN_SYSTEM = (
    "You are the project manager and senior engineer. You have READ-ONLY access "
    "to the repository. Study it and produce a concrete implementation plan: "
    "files to touch, approach, risks, how to verify. Do not modify anything."
)
DEV_SYSTEM = (
    "You are a software engineer working inside a git worktree (your current "
    "directory). Implement the task with minimal, focused changes and run the "
    "test command to verify your work. Never modify protected paths. Do not "
    "run git commit or git push; the orchestrator commits for you. Finish with "
    "a short summary of what you changed."
)
REVIEW_SYSTEM = (
    "You are a strict code reviewer with READ-ONLY access. Judge the diff "
    "against the task and acceptance criteria: correctness, missed edge cases, "
    "tests weakened to pass, unrelated changes. Do not modify any file. "
    "End your reply with exactly one JSON object on its own line:\n"
    '{"verdict": "approve" | "request_changes", "comments": ["..."]}'
)


class BudgetExceeded(Exception):
    pass


def parse_verdict(text: str) -> Tuple[bool, List[str]]:
    """Last JSON object containing "verdict". Unparseable counts as request_changes."""
    for match in reversed(re.findall(r"\{[^{}]*\"verdict\"[^{}]*\}", text or "", re.S)):
        try:
            data = json.loads(match)
        except json.JSONDecodeError:
            continue
        comments = [str(c) for c in data.get("comments", [])]
        return data.get("verdict") == "approve", comments
    return False, ["Reviewer gave no parseable verdict. Raw output:\n" + (text or "")[-2000:]]


def task_text(spec: TaskSpec) -> str:
    criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none given)"
    protected = "\n".join(f"- {p}" for p in spec.protected_paths) or "- (none)"
    return (
        f"# Task: {spec.title}\n\n{spec.description}\n\n"
        f"## Acceptance criteria\n{criteria}\n\n"
        f"## Test command (the quality gate)\n{spec.test_command}\n\n"
        f"## Protected paths (must NOT be modified)\n{protected}\n"
    )


class Pipeline:
    def __init__(self, cfg: Config, store: Store, ws: Workspace,
                 agents: Dict[str, Agent], chat_fn: ChatFn = chat,
                 memory: Optional[Memory] = None):
        self.cfg, self.store, self.ws = cfg, store, ws
        self.agents, self.chat_fn = agents, chat_fn
        self.memory: Memory = memory or NullMemory()

    # -- helpers ---------------------------------------------------------
    def _check_budget(self, tid: str, spec: TaskSpec) -> None:
        spent = self.store.get(tid)["cost_usd"]
        if spent >= spec.budget_usd:
            raise BudgetExceeded(f"spent ${spent:.4f} of ${spec.budget_usd:.2f}")

    def _call(self, tid: str, spec: TaskSpec, agent_name: str, role: str,
              prompt: str, system: str, wt: Path, read_only: bool) -> Delivery:
        self._check_budget(tid, spec)
        if agent_name not in self.agents:
            raise RuntimeError(f"agent '{agent_name}' is not defined in config")
        req = AgentRequest(
            role=role, prompt=prompt, system_prompt=system, worktree=wt,
            read_only=read_only, max_turns=self.cfg.limits.max_turns,
            timeout_s=self.cfg.limits.agent_timeout_s,
        )
        self.store.event(tid, "agent_start", {"agent": agent_name, "role": role})
        try:
            d = self.agents[agent_name].run(req)
        except Exception as exc:  # noqa: BLE001 - becomes a failed delivery
            d = Delivery(success=False, error=repr(exc))
        d.agent, d.role = agent_name, role
        self.store.add_cost(tid, d.cost_usd)
        # Enforced here, not trusted to the harness: read-only means read-only.
        if read_only and self.ws.is_dirty(wt):
            self.ws.discard(wt)
            self.store.event(tid, "readonly_violation", {"agent": agent_name, "role": role})
        self.store.event(tid, "agent_end", d.model_dump())
        return d

    def _plan(self, tid: str, spec: TaskSpec, wt: Path) -> Optional[str]:
        """Planning is best-effort: a failed plan never fails the task."""
        self.store.set_state(tid, C.PLANNING)
        planner = self.cfg.roles.planner
        # Hints go to planners only. With nobody to read them, memory is not asked.
        hints = self._memory_hints(tid, spec) if (spec.use_moa or planner) else ""
        if spec.use_moa:
            self._check_budget(tid, spec)
            try:
                text = (task_text(spec) + hints
                        + "\n## Repository files\n" + self.ws.file_list(wt))
                plan, cost, notes = run_moa(self.cfg.moa, text, self.chat_fn)
                self.store.add_cost(tid, cost)
                self.store.event(tid, "moa", {"cost_usd": cost, "proposers": notes, "plan": plan})
                return plan
            except Exception as exc:  # noqa: BLE001
                self.store.event(tid, "moa_failed", {"error": repr(exc)})
        if planner:
            d = self._call(tid, spec, planner, "planner", task_text(spec) + hints,
                           PLAN_SYSTEM, wt, True)
            if d.success and d.summary.strip():
                return d.summary
        return None

    # -- memory ----------------------------------------------------------
    def _memory_hints(self, tid: str, spec: TaskSpec) -> str:
        """Rendered hints for the planner. Memory errors mean 'no hints'."""
        try:
            ctx = self.memory.get_context(spec)
        except Exception as exc:  # noqa: BLE001
            self.store.event(tid, "memory_failed", {"where": "get_context", "error": repr(exc)})
            ctx = MemoryContext()
        ctx.hints = ctx.hints[:MAX_HINTS]
        self.store.add_cost(tid, ctx.cost_usd)
        self.store.event(tid, "memory_context", ctx.model_dump())
        return ctx.render()

    def _postmortem(self, tid: str, spec: TaskSpec, wt: Path, feedback: str) -> None:
        """Read-only failure attribution. Skipped when the budget is gone; never
        changes the task's outcome."""
        try:
            prompt = (task_text(spec) + "\n## Feedback on the final attempt\n" + feedback
                      + "\n\n## Final diff\n```diff\n" + self.ws.diff(wt, spec.base_branch)
                      + "\n```\n")
            d = self._call(tid, spec, self.cfg.roles.reviewer, "postmortem",
                           prompt, POSTMORTEM_SYSTEM, wt, True)
            if d.success:
                self.store.event(tid, "attribution", parse_attribution(d.summary).model_dump())
        except Exception as exc:  # noqa: BLE001 - includes BudgetExceeded
            self.store.event(tid, "postmortem_skipped", {"reason": repr(exc)})

    def _record_outcome(self, tid: str, spec: TaskSpec) -> None:
        try:
            self.memory.record_outcome(
                outcome_from_events(spec, self.store.get(tid), self.store.events(tid)))
        except Exception as exc:  # noqa: BLE001
            self.store.event(tid, "memory_failed", {"where": "record_outcome", "error": repr(exc)})

    # -- main ------------------------------------------------------------
    def run_task(self, tid: str) -> str:
        spec = self.store.spec(tid)
        try:
            self._run(tid, spec)
        except BudgetExceeded as exc:
            self.store.set_state(tid, C.BUDGET_EXCEEDED, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.store.set_state(tid, C.FAILED, repr(exc))
        self._record_outcome(tid, spec)
        return self.store.get(tid)["state"]

    def _run(self, tid: str, spec: TaskSpec) -> None:
        roles = self.cfg.roles
        wt = self.ws.create(spec)
        plan = self._plan(tid, spec, wt)

        self.store.set_state(tid, C.DEVELOPING)
        prompt = task_text(spec) + (f"\n## Plan from the tech lead\n{plan}\n" if plan else "")
        d = self._call(tid, spec, roles.developer, "developer", prompt, DEV_SYSTEM, wt, False)
        self.ws.commit_all(wt, f"[{roles.developer}] {spec.title}")
        if not d.success:
            self.store.set_state(tid, C.FAILED, f"developer failed: {d.error}")
            return

        for round_no in range(spec.max_repair_rounds + 1):
            self.store.set_state(tid, C.GATING, f"round {round_no}")
            gate = run_gate(self.ws, wt, spec)
            self.store.event(tid, "gate", gate.model_dump())

            if gate.passed:
                self.store.set_state(tid, C.REVIEWING, f"round {round_no}")
                review = self._call(tid, spec, roles.reviewer, "reviewer",
                                    self._review_prompt(spec, wt, gate), REVIEW_SYSTEM, wt, True)
                if not review.success:
                    self.store.set_state(tid, C.FAILED, f"reviewer failed: {review.error}")
                    return
                approved, comments = parse_verdict(review.summary)
                self.store.event(tid, "review", {"approved": approved, "comments": comments})
                if approved:
                    self.store.set_state(
                        tid, C.AWAITING_APPROVAL,
                        f"gate passed, reviewer approved; branch {self.ws.branch(spec)}")
                    return
                feedback = "The reviewer requested changes:\n" + "\n".join(f"- {c}" for c in comments)
            else:
                feedback = self._gate_feedback(gate)

            if round_no == spec.max_repair_rounds:
                break
            last = round_no == spec.max_repair_rounds - 1
            fixer = roles.escalation if (last and roles.escalation) else roles.repairer
            self.store.set_state(tid, C.REPAIRING, f"round {round_no + 1} by {fixer}")
            fix_prompt = task_text(spec) + "\n## Your previous attempt was not accepted\n" + feedback
            d = self._call(tid, spec, fixer, "repairer", fix_prompt, DEV_SYSTEM, wt, False)
            self.ws.commit_all(wt, f"[{fixer}] repair round {round_no + 1}")
            if not d.success:
                self.store.set_state(tid, C.FAILED, f"repairer failed: {d.error}")
                return

        self._postmortem(tid, spec, wt, feedback)
        self.store.set_state(tid, C.FAILED,
                             f"not accepted after {spec.max_repair_rounds} repair rounds")

    def _review_prompt(self, spec: TaskSpec, wt: Path, gate: GateReport) -> str:
        return (
            task_text(spec)
            + "\n## Quality gate\nPASSED. Output tail:\n" + gate.output_tail[-1500:]
            + "\n\n## Diff under review\n```diff\n" + self.ws.diff(wt, spec.base_branch) + "\n```\n"
        )

    @staticmethod
    def _gate_feedback(gate: GateReport) -> str:
        if gate.reason == "no_changes":
            return "You made no changes to the repository. Implement the task."
        parts = [f"The quality gate failed ({gate.reason})."]
        if gate.protected_violations:
            parts.append("You modified protected paths. Restore them exactly "
                         "(git checkout <base> -- <path>): " + ", ".join(gate.protected_violations))
        if gate.reason in ("tests_failed", "timeout"):
            parts.append(f"Test command exit code: {gate.exit_code}. Output tail:\n{gate.output_tail}")
        return "\n".join(parts)
