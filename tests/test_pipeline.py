from pathlib import Path

from orchestrator import contract as C
from orchestrator.config import MoAClient, MoAConfig
from orchestrator.contract import Delivery
from orchestrator.gate import protected_hits, scrubbed_env
from orchestrator.pipeline import Pipeline, parse_verdict
from orchestrator.selftest import (APPROVE, FnAgent, default_agents, demo_config,
                                   demo_spec, make_demo_repo, run_selftest, write_calc)
from orchestrator.store import Store
from orchestrator.workspace import Workspace


def setup(tmp: Path, agents=None, cfg=None, chat_fn=None, memory=None):
    ws = Workspace(tmp)
    store = Store(tmp / "state" / "db.sqlite")
    make_demo_repo(ws)
    kwargs = {"chat_fn": chat_fn} if chat_fn else {}
    kwargs["memory"] = memory
    pipe = Pipeline(cfg or demo_config(), store, ws, agents or default_agents(), **kwargs)
    return ws, store, pipe


def test_selftest_happy_path_with_repair(tmp_path):
    run_selftest(tmp_path)


def test_protected_path_edit_is_blocked(tmp_path):
    def cheat(req):
        # "Fix" the task by weakening the acceptance test instead of the code.
        (req.worktree / "tests/acceptance/test_calc.py").write_text("")
        return Delivery(summary="done", cost_usd=0.01)

    agents = default_agents()
    agents["dev"] = FnAgent(cheat)
    agents["dev2"] = FnAgent(lambda r: Delivery(summary="nothing"))
    ws, store, pipe = setup(tmp_path, agents)
    tid = store.add_task(demo_spec(max_repair_rounds=1))
    assert pipe.run_task(tid) == C.FAILED
    gates = [e["payload"] for e in store.events(tid) if e["kind"] == "gate"]
    assert all('"protected_paths"' in g for g in gates)
    assert "a - b" in (ws.repos / "demo" / "calc.py").read_text()


def test_budget_stops_the_task(tmp_path):
    ws, store, pipe = setup(tmp_path)
    # planner 0.01 + dev 0.01 -> next call is refused
    tid = store.add_task(demo_spec(budget_usd=0.015))
    assert pipe.run_task(tid) == C.BUDGET_EXCEEDED


def test_reviewer_change_request_triggers_repair(tmp_path):
    verdicts = iter(['{"verdict": "request_changes", "comments": ["add a comment"]}', APPROVE])
    seen = []

    def repair(req):
        seen.append(req.prompt)
        (req.worktree / "calc.py").write_text("def add(a, b):\n    # sum\n    return a + b\n")
        return Delivery(summary="commented")

    agents = default_agents()
    agents["dev"] = FnAgent(write_calc("a + b"))
    agents["rev"] = FnAgent(lambda r: Delivery(summary=next(verdicts)))
    agents["dev2"] = FnAgent(repair)
    ws, store, pipe = setup(tmp_path, agents)
    tid = store.add_task(demo_spec())
    assert pipe.run_task(tid) == C.AWAITING_APPROVAL
    assert "add a comment" in seen[0]


def test_failed_planner_is_not_fatal(tmp_path):
    def boom(req):
        raise RuntimeError("codex not installed")

    agents = default_agents()
    agents["pm"] = FnAgent(boom)
    agents["dev"] = FnAgent(write_calc("a + b"))
    ws, store, pipe = setup(tmp_path, agents)
    tid = store.add_task(demo_spec())
    assert pipe.run_task(tid) == C.AWAITING_APPROVAL


def test_moa_plan_reaches_developer(tmp_path):
    client = dict(kind="openai_compatible", model="m", base_url="http://x", key_env="K")
    cfg = demo_config()
    cfg.moa = MoAConfig(
        proposers=[MoAClient(name="a", **client), MoAClient(name="b", **client),
                   MoAClient(name="unset", **{**client, "model": ""})],
        aggregator=MoAClient(name="agg", **client),
    )

    def fake_chat(c, system, user):
        if c.name == "b":
            raise RuntimeError("api down")
        return ("MERGED-PLAN" if c.name == "agg" else "proposal"), 0.001

    prompts = []

    def dev(req):
        prompts.append(req.prompt)
        return write_calc("a + b")(req)

    agents = default_agents()
    agents["dev"] = FnAgent(dev)
    ws, store, pipe = setup(tmp_path, agents, cfg, fake_chat)
    tid = store.add_task(demo_spec(use_moa=True))
    assert pipe.run_task(tid) == C.AWAITING_APPROVAL
    assert "MERGED-PLAN" in prompts[0]


def test_parse_verdict():
    assert parse_verdict('ok\n{"verdict": "approve", "comments": []}') == (True, [])
    ok, comments = parse_verdict('{"verdict": "request_changes", "comments": ["x"]}')
    assert (ok, comments) == (False, ["x"])
    assert parse_verdict("no json here")[0] is False


def test_gate_helpers(monkeypatch):
    assert protected_hits(["tests/acceptance/a/b.py", "src/x.py"], ["tests/acceptance/**"]) == [
        "tests/acceptance/a/b.py"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    env = scrubbed_env(keep=["DEEPSEEK_API_KEY"])
    assert "DEEPSEEK_API_KEY" in env and "OPENAI_API_KEY" not in env


def test_subprocess_agent_reports_missing_harness(tmp_path):
    from orchestrator.agents.base import SubprocessAgent
    from orchestrator.contract import AgentRequest

    agent = SubprocessAgent("x", {"type": "nope"})
    d = agent.run(AgentRequest(role="developer", prompt="p", system_prompt="s",
                               worktree=tmp_path, read_only=False, timeout_s=60))
    assert d.success is False and "unknown agent type" in d.error


# -- memory interface ----------------------------------------------------
from orchestrator.memory import Hint, MemoryContext  # noqa: E402


class FakeMemory:
    def __init__(self, n_hints=5, broken=False):
        self.n, self.broken, self.asked, self.outcomes = n_hints, broken, 0, []

    def get_context(self, spec):
        self.asked += 1
        if self.broken:
            raise RuntimeError("memory down")
        hints = [Hint(id=f"h{i}", kind="candidate", claim=f"SECRET-CLAIM-{i}",
                      conditions="unknown", evidence="task 0000", verify="run the tests")
                 for i in range(1, self.n + 1)]
        return MemoryContext(hints=hints, cost_usd=0.002)

    def record_outcome(self, outcome):
        if self.broken:
            raise RuntimeError("memory down")
        self.outcomes.append(outcome)


def test_hints_reach_planner_only_and_outcome_is_recorded(tmp_path):
    prompts = {}

    def pm(req):
        prompts["pm"] = req.prompt
        return Delivery(summary='Plan: use +.\n{"hints": {"h1": "adopted", "h2": "rejected"}}',
                        cost_usd=0.01)

    def dev(req):
        prompts["dev"] = req.prompt
        return write_calc("a + b + 1")(req)

    agents = default_agents()
    agents["pm"], agents["dev"] = FnAgent(pm), FnAgent(dev)
    mem = FakeMemory()
    ws, store, pipe = setup(tmp_path, agents, memory=mem)
    tid = store.add_task(demo_spec())
    assert pipe.run_task(tid) == C.AWAITING_APPROVAL

    assert "SECRET-CLAIM-3" in prompts["pm"] and "SECRET-CLAIM-4" not in prompts["pm"]
    assert "SECRET-CLAIM" not in prompts["dev"]
    (out,) = mem.outcomes
    assert out.hints == {"h1": "adopted", "h2": "rejected", "h3": "unreported"}
    assert out.final_state == C.AWAITING_APPROVAL and out.attribution is None
    assert [r.gate_passed for r in out.rounds] == [False, True] and out.repair_rounds == 1
    assert out.rounds[1].fixer == "dev2" and out.rounds[1].review_approved
    assert abs(out.cost_usd - 0.042) < 1e-9  # memory 0.002 + pm, dev, dev2, rev


def test_broken_memory_never_fails_a_task(tmp_path):
    ws, store, pipe = setup(tmp_path, memory=FakeMemory(broken=True))
    tid = store.add_task(demo_spec())
    assert pipe.run_task(tid) == C.AWAITING_APPROVAL
    wheres = [e["payload"] for e in store.events(tid) if e["kind"] == "memory_failed"]
    assert len(wheres) == 2


def test_memory_not_asked_without_a_planner(tmp_path):
    cfg = demo_config()
    cfg.roles.planner = None
    mem = FakeMemory()
    ws, store, pipe = setup(tmp_path, cfg=cfg, memory=mem)
    tid = store.add_task(demo_spec())
    assert pipe.run_task(tid) == C.AWAITING_APPROVAL
    assert mem.asked == 0 and mem.outcomes[0].hints == {}


def test_failed_task_gets_a_postmortem(tmp_path):
    def rev(req):
        assert req.role == "postmortem" and req.read_only
        return Delivery(summary='{"cause": "method", "blocker": "off by one", "evidence": "AssertionError"}')

    agents = default_agents()
    agents["dev2"] = FnAgent(write_calc("a + b + 2"))
    agents["rev"] = FnAgent(rev)
    mem = FakeMemory(n_hints=0)
    ws, store, pipe = setup(tmp_path, agents, memory=mem)
    tid = store.add_task(demo_spec(max_repair_rounds=1))
    assert pipe.run_task(tid) == C.FAILED
    att = mem.outcomes[0].attribution
    assert att.cause == "method" and att.blocker == "off by one" and att.confirmed is True


def test_postmortem_is_skipped_when_budget_is_gone(tmp_path):
    agents = default_agents()
    agents["dev2"] = FnAgent(write_calc("a + b + 2"))
    mem = FakeMemory(n_hints=0)
    ws, store, pipe = setup(tmp_path, agents, memory=mem)
    # pm 0.01 + dev 0.01 + dev2 0.01 = 0.03 -> nothing left for the postmortem
    tid = store.add_task(demo_spec(max_repair_rounds=1, budget_usd=0.025))
    assert pipe.run_task(tid) == C.FAILED
    assert mem.outcomes[0].attribution is None
    assert any(e["kind"] == "postmortem_skipped" for e in store.events(tid))
