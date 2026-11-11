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


def setup(tmp: Path, agents=None, cfg=None, chat_fn=None):
    ws = Workspace(tmp)
    store = Store(tmp / "state" / "db.sqlite")
    make_demo_repo(ws)
    kwargs = {"chat_fn": chat_fn} if chat_fn else {}
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
