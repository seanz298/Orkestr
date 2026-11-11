"""Git isolation: every task gets its own worktree and its own branch."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List

from .contract import TaskSpec

_IDENT = ["-c", "user.name=ai-dev-team", "-c", "user.email=ai-dev-team@localhost"]


def git(args: List[str], cwd: Path, check: bool = True) -> str:
    p = subprocess.run(
        ["git", *_IDENT, *args], cwd=str(cwd), capture_output=True, text=True
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.repos = root / "repos"
        self.worktrees = root / "worktrees"
        self.repos.mkdir(parents=True, exist_ok=True)
        self.worktrees.mkdir(parents=True, exist_ok=True)

    def repo_path(self, name: str) -> Path:
        path = self.repos / name
        if not (path / ".git").exists():
            raise RuntimeError(f"repo '{name}' not found; run `add-repo` first")
        return path

    def branch(self, spec: TaskSpec) -> str:
        return f"ai/{spec.id}"

    def worktree(self, spec: TaskSpec) -> Path:
        return self.worktrees / spec.id

    def create(self, spec: TaskSpec) -> Path:
        repo = self.repo_path(spec.repo)
        wt = self.worktree(spec)
        self.remove(spec, delete_branch=True)
        git(["worktree", "add", "-b", self.branch(spec), str(wt), spec.base_branch], repo)
        return wt

    def remove(self, spec: TaskSpec, delete_branch: bool = False) -> None:
        repo = self.repo_path(spec.repo)
        wt = self.worktree(spec)
        if wt.exists():
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)
        git(["worktree", "prune"], repo, check=False)
        if delete_branch:
            git(["branch", "-D", self.branch(spec)], repo, check=False)

    def is_dirty(self, wt: Path) -> bool:
        return bool(git(["status", "--porcelain"], wt).strip())

    def discard(self, wt: Path) -> None:
        git(["reset", "--hard"], wt)
        git(["clean", "-fd"], wt)

    def commit_all(self, wt: Path, message: str) -> bool:
        if not self.is_dirty(wt):
            return False
        git(["add", "-A"], wt)
        git(["commit", "-m", message], wt)
        return True

    def changed_files(self, wt: Path, base: str) -> List[str]:
        out = git(["diff", "--name-only", f"{base}...HEAD"], wt)
        return [line for line in out.splitlines() if line.strip()]

    def diff(self, wt: Path, base: str, max_chars: int = 30000) -> str:
        out = git(["diff", f"{base}...HEAD"], wt)
        if len(out) > max_chars:
            out = out[:max_chars] + "\n... [diff truncated]"
        return out

    def file_list(self, wt: Path, limit: int = 300) -> str:
        files = git(["ls-files"], wt).splitlines()
        more = f"\n... (+{len(files) - limit} more)" if len(files) > limit else ""
        return "\n".join(files[:limit]) + more

    def merge(self, spec: TaskSpec) -> None:
        """Human-approved merge of the task branch into the base branch (local only)."""
        repo = self.repo_path(spec.repo)
        if git(["status", "--porcelain"], repo).strip():
            raise RuntimeError("base checkout has uncommitted changes; refusing to merge")
        git(["checkout", spec.base_branch], repo)
        git(["merge", "--no-ff", "-m", f"Merge {self.branch(spec)}: {spec.title}",
             self.branch(spec)], repo)
