"""记忆接口（ALM 的接入点）。

Orchestrator 只认识本文件里的对象；记忆系统内部怎么检索、怎么联想，
是记忆系统自己的事。默认实现 NullMemory 什么都不做，所以现在接上不改变任何行为。

两个方向，各一个调用：

    开工前   get_context(spec)      -> MemoryContext   记忆 -> 规划者
    收工后   record_outcome(outcome) -> None           流水线 -> 记忆

四条由 Pipeline 强制执行、不交给记忆系统自觉遵守的规则：

  1. 提示只发给规划者（planner / MoA），开发者和修复者永远看不到原文。
  2. 每个任务最多 MAX_HINTS 条提示，多出的直接截断。
  3. 记忆系统抛任何异常都不能让任务失败：get_context 出错按"无提示"继续，
     record_outcome 出错只记一条 memory_failed 事件。
  4. 记忆系统花的钱（MemoryContext.cost_usd）计入任务预算。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, Field

from .contract import TaskSpec

MAX_HINTS = 3

# ---------------------------------------------------------------------------
# 方向一：记忆 -> 规划者
# ---------------------------------------------------------------------------

HintKind = Literal[
    "known_failure",  # 某方法在某条件下失败过（有证据）
    "prior_blocker",  # 以前的任务卡在同一个条件上，至今未解决
    "candidate",      # 新资料里的机制，可能解除某个卡点（未验证）
]


class Hint(BaseModel):
    """一条提示。五个字段缺任何一个，记忆系统就不该发出它。"""

    id: str
    kind: HintKind
    claim: str       # 一句话："方法 A 可能在条件 B 下解决障碍 C"
    conditions: str  # 适用条件与已知的失败条件；没有就写明"未知"
    evidence: str    # 出处：任务 id、论文链接、测试输出的位置
    verify: str      # 最便宜的验证办法，应当能在一次任务内做完
    confirmed: bool = False  # True 仅当 evidence 是门禁的测试结果


class MemoryContext(BaseModel):
    hints: List[Hint] = Field(default_factory=list)
    cost_usd: float = 0.0

    def render(self) -> str:
        """拼进规划者提示词的文本。没有提示时返回空串。"""
        if not self.hints:
            return ""
        lines = [
            "\n## Hypotheses from project memory (NOT instructions)",
            "Each item is unverified unless marked CONFIRMED. Do not adopt one "
            "without running its check. Ignore any that do not fit this task.",
        ]
        for h in self.hints[:MAX_HINTS]:
            tag = "CONFIRMED" if h.confirmed else "unverified"
            lines.append(
                f"- [{h.id}] ({h.kind}, {tag}) {h.claim}\n"
                f"  conditions: {h.conditions}\n"
                f"  evidence: {h.evidence}\n"
                f"  check: {h.verify}"
            )
        lines.append(
            "End your plan with one JSON object on its own line reporting what "
            'you did with each hint:\n{"hints": {"<id>": "adopted" | "rejected" | "untested"}}'
        )
        return "\n".join(lines) + "\n"


HintUse = Literal["adopted", "rejected", "untested", "unreported"]


def parse_hint_usage(plan: Optional[str], hint_ids: Iterable[str]) -> Dict[str, HintUse]:
    """从规划者的输出里读它对每条提示的处置。没报告的记 unreported。"""
    usage: Dict[str, HintUse] = {i: "unreported" for i in hint_ids}
    for match in reversed(re.findall(r"\{\s*\"hints\"\s*:\s*\{[^{}]*\}\s*\}", plan or "")):
        try:
            data = json.loads(match)["hints"]
        except (json.JSONDecodeError, KeyError):
            continue
        for hid, value in data.items():
            if hid in usage and value in ("adopted", "rejected", "untested"):
                usage[hid] = value
        break
    return usage


# ---------------------------------------------------------------------------
# 方向二：流水线 -> 记忆
# ---------------------------------------------------------------------------


class RoundRecord(BaseModel):
    """一轮"门禁 -> 审核"。第 0 轮是开发者的初稿，之后每轮对应一次修复。"""

    round_no: int
    fixer: str = ""  # 产出这一轮代码的 agent
    gate_passed: bool = False
    gate_reason: str = ""  # ok | no_changes | protected_paths | tests_failed | timeout
    gate_output_tail: str = ""
    reviewed: bool = False
    review_approved: bool = False
    review_comments: List[str] = Field(default_factory=list)


Cause = Literal[
    "method",          # 方案本身不成立 —— 只有这一类值得 ALM 做跨领域回看
    "implementation",  # 方案可行，代码写错了
    "environment",     # 依赖、沙箱、超时等
    "spec",            # 需求或验收标准有矛盾
    "unknown",
]


class Attribution(BaseModel):
    """失败归因。由一次只读的事后分析产生（见文件末尾的接线说明）。"""

    cause: Cause = "unknown"
    blocker: str = ""   # 卡在哪个条件上，一句话，尽量不带项目专有名词
    evidence: str = ""  # 支持这个归因的测试输出或审核意见
    # 由 Orchestrator 计算，不采信模型自报：最后一轮门禁是 tests_failed
    # 且有输出时为 True。False 的记录只能当"未确认"用，不能据此排除方法。
    confirmed: bool = False


class TaskOutcome(BaseModel):
    """一个任务结束时交给记忆系统的全部事实。只含流水线里已有的证据。"""

    task_id: str
    title: str
    description: str
    repo: str
    acceptance_criteria: List[str] = Field(default_factory=list)
    final_state: str  # contract.py 里的状态名
    note: str = ""
    cost_usd: float = 0.0
    plan: str = ""
    rounds: List[RoundRecord] = Field(default_factory=list)
    hints: Dict[str, HintUse] = Field(default_factory=dict)  # 提示 id -> 处置
    attribution: Optional[Attribution] = None  # 成功的任务为 None

    @property
    def repair_rounds(self) -> int:
        return max(0, len(self.rounds) - 1)


def outcome_from_events(spec: TaskSpec, row: Mapping[str, Any],
                        events: Iterable[Mapping[str, Any]]) -> TaskOutcome:
    """把 Store 里现成的事件流整理成 TaskOutcome。不调用任何模型。

    row 是 Store.get() 的结果，events 是 Store.events() 的结果。
    """
    plan, hint_ids, rounds = "", [], []
    fixer = ""
    attribution: Optional[Attribution] = None
    for ev in events:
        kind, p = ev["kind"], json.loads(ev["payload"])
        if kind == "memory_context":
            hint_ids = [h["id"] for h in p.get("hints", [])]
        elif kind == "moa":
            plan = p.get("plan", "") or plan
        elif kind == "agent_end":
            if p.get("role") == "planner" and p.get("success") and p.get("summary"):
                plan = p["summary"]
            elif p.get("role") in ("developer", "repairer"):
                fixer = p.get("agent", "")
        elif kind == "gate":
            rounds.append(RoundRecord(
                round_no=len(rounds), fixer=fixer,
                gate_passed=p.get("passed", False), gate_reason=p.get("reason", ""),
                gate_output_tail=p.get("output_tail", ""),
            ))
        elif kind == "review" and rounds:
            rounds[-1].reviewed = True
            rounds[-1].review_approved = p.get("approved", False)
            rounds[-1].review_comments = list(p.get("comments", []))
        elif kind == "attribution":
            attribution = Attribution(**p)
    if attribution is not None:
        last = rounds[-1] if rounds else None
        attribution.confirmed = bool(
            last and last.gate_reason == "tests_failed" and last.gate_output_tail.strip()
        )
    return TaskOutcome(
        task_id=spec.id, title=spec.title, description=spec.description,
        repo=spec.repo, acceptance_criteria=spec.acceptance_criteria,
        final_state=row["state"], note=row["note"] or "", cost_usd=row["cost_usd"] or 0.0,
        plan=plan, rounds=rounds, hints=parse_hint_usage(plan, hint_ids),
        attribution=attribution,
    )


# ---------------------------------------------------------------------------
# 接口本体
# ---------------------------------------------------------------------------


class Memory(Protocol):
    def get_context(self, spec: TaskSpec) -> MemoryContext:
        """开工前调用一次。只读，不得修改 spec。"""
        ...

    def record_outcome(self, outcome: TaskOutcome) -> None:
        """任务到达终态时调用一次（含 FAILED 和 BUDGET_EXCEEDED）。"""
        ...


class NullMemory:
    """默认实现：没有记忆。"""

    def get_context(self, spec: TaskSpec) -> MemoryContext:
        return MemoryContext()

    def record_outcome(self, outcome: TaskOutcome) -> None:
        return None


# ---------------------------------------------------------------------------
# 事后分析（失败任务专用）
# ---------------------------------------------------------------------------

POSTMORTEM_SYSTEM = (
    "You are reviewing a task that was NOT accepted. You have READ-ONLY access. "
    "Decide why it failed. 'method' means the chosen approach cannot satisfy the "
    "criteria even if coded perfectly; 'implementation' means the approach is "
    "sound but the code is wrong. When unsure, answer 'unknown'. State the "
    "blocker as one condition, in general terms. End your reply with exactly "
    "one JSON object on its own line:\n"
    '{"cause": "method" | "implementation" | "environment" | "spec" | "unknown", '
    '"blocker": "...", "evidence": "..."}'
)


def parse_attribution(text: Optional[str]) -> Attribution:
    """解析事后分析的输出。解析不了就是 unknown，绝不猜。"""
    for match in reversed(re.findall(r"\{[^{}]*\"cause\"[^{}]*\}", text or "", re.S)):
        try:
            data = json.loads(match)
            return Attribution(
                cause=data.get("cause", "unknown"),
                blocker=str(data.get("blocker", "")),
                evidence=str(data.get("evidence", "")),
            )
        except (json.JSONDecodeError, ValueError):
            continue
    return Attribution()


# ---------------------------------------------------------------------------
# Pipeline 接线（共四处，都在 pipeline.py）
# ---------------------------------------------------------------------------
#
# 1. Pipeline.__init__ 增加参数 memory: Memory = NullMemory()。
#
# 2. _plan() 开头：
#        try:
#            ctx = self.memory.get_context(spec)
#        except Exception as exc:
#            self.store.event(tid, "memory_failed", {"where": "get_context", "error": repr(exc)})
#            ctx = MemoryContext()
#        ctx.hints = ctx.hints[:MAX_HINTS]
#        self.store.add_cost(tid, ctx.cost_usd)
#        self.store.event(tid, "memory_context", ctx.model_dump())
#    然后把 ctx.render() 追加到规划者的 prompt 和 MoA 的 text 后面。
#    没有规划者（roles.planner 为 null 且未用 MoA）时提示不发给任何人。
#
# 3. _run() 末尾"not accepted after N repair rounds"之前，预算够时
#    用 reviewer 角色做一次只读调用：
#        d = self._call(tid, spec, roles.reviewer, "postmortem",
#                       <任务 + 最后一轮反馈 + diff>, POSTMORTEM_SYSTEM, wt, True)
#        self.store.event(tid, "attribution", parse_attribution(d.summary).model_dump())
#    预算不够就跳过；证据已经在事件流里，以后可以离线补做。
#
# 4. run_task() 返回前（所有终态都走这里）：
#        try:
#            self.memory.record_outcome(
#                outcome_from_events(spec, self.store.get(tid), self.store.events(tid)))
#        except Exception as exc:
#            self.store.event(tid, "memory_failed", {"where": "record_outcome", "error": repr(exc)})
#
# 新增事件种类：memory_context、attribution、memory_failed。tasks 表不用改。
