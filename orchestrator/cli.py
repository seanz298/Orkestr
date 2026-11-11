from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import yaml

from . import contract as C
from .config import config_path, load_config, workspace_root
from .contract import TaskSpec
from .store import Store
from .workspace import Workspace, git


def _store() -> Store:
    return Store(workspace_root() / "state" / "orchestrator.db")


def _ws() -> Workspace:
    return Workspace(workspace_root())


def cmd_run(args) -> int:
    from .agents import build_agents
    from .pipeline import Pipeline

    cfg, store, ws = load_config(), _store(), _ws()
    pipe = Pipeline(cfg, store, ws, build_agents(cfg))
    n = store.mark_interrupted()
    print(f"[orkestr] worker started ({n} interrupted task(s) marked)", flush=True)
    while True:
        tid = store.next_queued()
        if tid is None:
            if args.once:
                return 0
            time.sleep(3)
            continue
        print(f"[orkestr] {tid} start", flush=True)
        state = pipe.run_task(tid)
        row = store.get(tid)
        print(f"[orkestr] {tid} -> {state} (${row['cost_usd']:.4f}) {row['note']}", flush=True)


def cmd_submit(args) -> int:
    spec = TaskSpec.model_validate(yaml.safe_load(Path(args.file).read_text(encoding="utf-8")))
    _ws().repo_path(spec.repo)  # fail early if the repo is missing
    tid = _store().add_task(spec)
    print(f"submitted {tid}")
    return 0


def cmd_status(args) -> int:
    store = _store()
    if args.id:
        row = store.get(args.id)
        if row is None:
            print("no such task")
            return 1
        spec = store.spec(args.id)
        print(f"{row['id']}  {row['state']}  ${row['cost_usd']:.4f} / ${spec.budget_usd:.2f}")
        print(f"title : {spec.title}\nrepo  : {spec.repo}  branch ai/{spec.id}\nnote  : {row['note']}")
        return 0
    for row in store.list():
        title = json.loads(row["spec"])["title"]
        print(f"{row['id']:<14}{row['state']:<19}${row['cost_usd']:<9.4f}{title}")
    return 0


def cmd_log(args) -> int:
    for ev in _store().events(args.id):
        ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
        payload = ev["payload"]
        if not args.full and len(payload) > 300:
            payload = payload[:300] + " ..."
        print(f"{ts} {ev['kind']:<18} {payload}")
    return 0


def cmd_diff(args) -> int:
    store, ws = _store(), _ws()
    spec = store.spec(args.id)
    print(git(["diff", f"{spec.base_branch}...{ws.branch(spec)}"], ws.repo_path(spec.repo)))
    return 0


def cmd_approve(args) -> int:
    store, ws = _store(), _ws()
    row = store.get(args.id)
    if row is None or row["state"] != C.AWAITING_APPROVAL:
        print(f"task is not awaiting approval (state: {row['state'] if row else 'missing'})")
        return 1
    spec = store.spec(args.id)
    ws.merge(spec)
    ws.remove(spec)
    store.set_state(args.id, C.APPROVED, f"merged into {spec.base_branch} (local; not pushed)")
    print(f"merged ai/{spec.id} into {spec.base_branch}. Nothing was pushed.")
    return 0


def cmd_reject(args) -> int:
    store, ws = _store(), _ws()
    spec = store.spec(args.id)
    ws.remove(spec)  # branch is kept for inspection
    store.set_state(args.id, C.REJECTED, args.reason)
    print("rejected")
    return 0


def cmd_retry(args) -> int:
    store = _store()
    if store.get(args.id) is None:
        print("no such task")
        return 1
    store.db.execute("UPDATE tasks SET cost_usd=0 WHERE id=?", (args.id,))
    store.set_state(args.id, C.QUEUED, "retry")
    print("requeued")
    return 0


def cmd_add_repo(args) -> int:
    ws = _ws()
    name = args.name or args.url.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = ws.repos / name
    if dest.exists():
        print(f"{name} already exists")
        return 1
    p = subprocess.run(["git", "clone", args.url, str(dest)])
    if p.returncode == 0:
        print(f"added repo '{name}' (use repo: {name} in task files)")
    return p.returncode


def cmd_doctor(args) -> int:
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'OK' if good else 'FAIL'}] {label}" + (f"  -> {hint}" if not good and hint else ""))

    root = workspace_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".probe").write_text("x")
        (root / ".probe").unlink()
        check(f"workspace writable: {root}", True)
    except OSError as exc:
        check(f"workspace writable: {root}", False, str(exc))
    check("git installed", shutil.which("git") is not None)
    try:
        cfg = load_config()
        check(f"config loads: {config_path()}", True)
    except Exception as exc:  # noqa: BLE001
        check(f"config loads: {config_path()}", False, repr(exc))
        return 1

    modules = {"deepagents": ["deepagents", "langchain_deepseek"],
               "claude": ["claude_agent_sdk"], "codex": []}
    used = {v for v in cfg.roles.model_dump().values() if v}
    for name in sorted(used):
        acfg = cfg.agents.get(name)
        if acfg is None:
            check(f"agent '{name}' defined in config", False)
            continue
        kind = acfg.get("type")
        for mod in modules.get(kind, []):
            check(f"{name}: python package {mod}", importlib.util.find_spec(mod) is not None,
                  "pip install failed? rebuild the image")
        if kind == "codex":
            check(f"{name}: codex CLI on PATH", shutil.which(acfg.get("binary", "codex")) is not None)
        for key in acfg.get("env_keys", []):
            check(f"{name}: {key} set", bool(os.environ.get(key)), "fill it in .env")
        if kind != "codex":
            model = acfg.get("model", "")
            check(f"{name}: model set ({model or 'empty'})", bool(model) and not model.endswith(":"),
                  "fill the model name in .env")
    print("\nAll good." if ok else "\nFix the FAIL lines, then run doctor again.")
    return 0 if ok else 1


def cmd_selftest(args) -> int:
    from .selftest import run_selftest

    with tempfile.TemporaryDirectory() as tmp:
        run_selftest(Path(tmp))
    print("selftest passed: git isolation, quality gate, repair loop, "
          "read-only enforcement and approval merge all work here.\n"
          "(This used scripted fake agents; the real harnesses are not covered.)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="orkestr")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="worker loop")
    s.add_argument("--once", action="store_true", help="exit when the queue is empty")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("submit", help="queue a task from a YAML file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("status", help="list tasks, or show one")
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("log", help="event log of a task")
    s.add_argument("id")
    s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("diff", help="diff of the task branch against base")
    s.add_argument("id")
    s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("approve", help="merge an accepted task into base (local)")
    s.add_argument("id")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("reject")
    s.add_argument("id")
    s.add_argument("-m", "--reason", default="rejected by owner")
    s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("retry", help="requeue a task from scratch")
    s.add_argument("id")
    s.set_defaults(fn=cmd_retry)
    s = sub.add_parser("add-repo", help="clone a repo into the workspace")
    s.add_argument("url")
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_add_repo)
    sub.add_parser("doctor", help="check keys, packages and tools").set_defaults(fn=cmd_doctor)
    sub.add_parser("selftest", help="offline end-to-end test").set_defaults(fn=cmd_selftest)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
