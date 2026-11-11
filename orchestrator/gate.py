"""Independent quality gate. Runs outside every agent, with secrets removed."""
from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List

from .contract import GateReport, TaskSpec
from .workspace import Workspace

_SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)


def scrubbed_env(keep: Iterable[str] = ()) -> Dict[str, str]:
    """Environment without API keys, except the ones explicitly allowed."""
    keep = set(keep)
    return {k: v for k, v in os.environ.items() if k in keep or not _SECRET.search(k)}


def protected_hits(files: List[str], patterns: List[str]) -> List[str]:
    hits = []
    for f in files:
        for p in patterns:
            if fnmatch.fnmatch(f, p) or f == p or f.startswith(p.rstrip("/*") + "/"):
                hits.append(f)
                break
    return hits


def run_gate(ws: Workspace, wt: Path, spec: TaskSpec) -> GateReport:
    files = ws.changed_files(wt, spec.base_branch)
    if not files:
        return GateReport(passed=False, reason="no_changes")
    violations = protected_hits(files, spec.protected_paths)

    proc = subprocess.Popen(
        spec.test_command, shell=True, cwd=str(wt), env=scrubbed_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=spec.gate_timeout_s)
        code, reason = proc.returncode, ""
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        code, reason = None, "timeout"
    # Tests must not leave junk that later gets committed.
    if ws.is_dirty(wt):
        ws.discard(wt)

    tail = (out or "")[-4000:]
    if violations:
        reason = "protected_paths"
    elif reason != "timeout" and code != 0:
        reason = "tests_failed"
    passed = not violations and code == 0
    return GateReport(
        passed=passed, reason="ok" if passed else reason, changed_files=files,
        protected_violations=violations, exit_code=code, output_tail=tail,
    )
