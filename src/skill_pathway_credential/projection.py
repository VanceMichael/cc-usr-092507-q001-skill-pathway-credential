"""投影：把事件折叠为可查询的领域状态，可重放到任意日期。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from .domain import ConclusionStatus, Decision, EvidenceStatus, ItemStatus, StudentStatus
from .events import Event


@dataclass
class StandardVersion:
    version: int
    credits: dict[str, int]
    effective_on: date


@dataclass
class StudentState:
    program: str
    stage: str
    status: str = StudentStatus.ACTIVE
    transitions: list[dict] = field(default_factory=list)


@dataclass
class PlanItem:
    skill: str
    standard_id: str
    standard_version: int
    required_credits: int
    status: str = ItemStatus.PENDING


@dataclass
class PlanState:
    stage: str
    items: dict[str, PlanItem] = field(default_factory=dict)


@dataclass
class EvidenceState:
    evidence_id: str
    student_id: str
    skill: str
    digest: str
    source_kind: str
    enterprise: str | None
    submitted_on: date
    due_on: date
    supersedes: str | None
    status: str = EvidenceStatus.PENDING
    reviewer: str | None = None


@dataclass
class ReviewerState:
    domains: set[str]
    conflict_students: set[str]
    conflict_enterprises: set[str]


@dataclass
class DecisionState:
    decision_id: str
    evidence_id: str
    reviewer: str
    decision: str
    reason: str
    recorded_on: date


@dataclass
class ConclusionState:
    conclusion_id: str
    evidence_id: str
    student_id: str
    skill: str
    standard_id: str
    standard_version: int
    credits: int
    status: str = ConclusionStatus.ACTIVE


@dataclass
class WaitEntry:
    student_id: str
    requested_on: date
    rank: int  # 0 = 岗位撤回受影响者优先，1 = 普通候补
    seq: int


@dataclass
class PostingState:
    enterprise: str
    skill: str
    capacity: int
    open: bool = True
    holders: dict[str, tuple[int, date]] = field(default_factory=dict)  # 学生 -> (占用序号, 申请日期)
    waitlist: list[WaitEntry] = field(default_factory=list)

    @property
    def free_capacity(self) -> int:
        return self.capacity - len(self.holders)

    def ordered_waitlist(self) -> list[WaitEntry]:
        return sorted(self.waitlist, key=lambda e: (e.rank, e.requested_on, e.seq))


class Projection:
    """全部领域状态；由事件折叠而来，重放截至某日的事件即得当日状态。"""

    def __init__(self) -> None:
        self.standards: dict[str, dict[int, StandardVersion]] = {}
        self.standard_current: dict[str, int] = {}
        self.students: dict[str, StudentState] = {}
        self.plans: dict[tuple[str, str], PlanState] = {}
        self.evidence: dict[str, EvidenceState] = {}
        self.evidence_by_digest: dict[str, str] = {}
        self.evidence_by_skill: dict[tuple[str, str], list[str]] = {}
        self.reviewers: dict[str, ReviewerState] = {}
        self.decisions: dict[str, DecisionState] = {}
        self.decision_by_evidence: dict[str, str] = {}
        self.conclusions: dict[str, ConclusionState] = {}
        self.credentials: dict[str, dict] = {}
        self.credential_by_digest: dict[str, str] = {}
        self.postings: dict[str, PostingState] = {}
        self.disclosures: dict[tuple[str, str], set[str]] = {}

    # ---- 查询辅助 ----
    def active_conclusion(self, student_id: str, skill: str) -> ConclusionState | None:
        for conclusion in self.conclusions.values():
            if (
                conclusion.student_id == student_id
                and conclusion.skill == skill
                and conclusion.status == ConclusionStatus.ACTIVE
            ):
                return conclusion
        return None

    def conclusions_for(self, student_id: str, skill: str) -> list[ConclusionState]:
        return [
            c
            for c in self.conclusions.values()
            if c.student_id == student_id and c.skill == skill
        ]

    def holding_posting(self, student_id: str) -> str | None:
        for posting_id, posting in self.postings.items():
            if student_id in posting.holders:
                return posting_id
        return None

    def open_assignments(self, reviewer_id: str) -> int:
        return sum(
            1
            for evidence in self.evidence.values()
            if evidence.reviewer == reviewer_id and evidence.status == EvidenceStatus.PENDING
        )

    # ---- 事件折叠 ----
    def apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)

    def _on_standard_published(self, event: Event) -> None:
        p = event.payload
        self.standards.setdefault(p["standard_id"], {})[p["version"]] = StandardVersion(
            version=p["version"],
            credits=dict(p["credits"]),
            effective_on=date.fromisoformat(p["effective_on"]),
        )
        self.standard_current[p["standard_id"]] = p["version"]

    def _on_plan_created(self, event: Event) -> None:
        p = event.payload
        if p["student"] not in self.students:
            self.students[p["student"]] = StudentState(program=p["program"], stage=p["stage"])
        plan = PlanState(stage=p["stage"])
        for item in p["items"]:
            plan.items[item["skill"]] = PlanItem(
                skill=item["skill"],
                standard_id=item["standard_id"],
                standard_version=item["standard_version"],
                required_credits=item["required_credits"],
            )
        self.plans[(p["student"], p["stage"])] = plan

    def _on_plan_item_recomputed(self, event: Event) -> None:
        p = event.payload
        item = self.plans[(p["student"], p["stage"])].items[p["skill"]]
        item.standard_version = p["standard_version"]
        item.required_credits = p["required_credits"]

    def _on_plan_item_completed(self, event: Event) -> None:
        p = event.payload
        self.plans[(p["student"], p["stage"])].items[p["skill"]].status = ItemStatus.COMPLETED

    def _on_evidence_submitted(self, event: Event) -> None:
        p = event.payload
        self.evidence[p["evidence_id"]] = EvidenceState(
            evidence_id=p["evidence_id"],
            student_id=p["student"],
            skill=p["skill"],
            digest=p["digest"],
            source_kind=p["source_kind"],
            enterprise=p.get("enterprise"),
            submitted_on=date.fromisoformat(p["submitted_on"]),
            due_on=date.fromisoformat(p["due_on"]),
            supersedes=p.get("supersedes"),
        )
        self.evidence_by_digest[p["digest"]] = p["evidence_id"]
        self.evidence_by_skill.setdefault((p["student"], p["skill"]), []).append(
            p["evidence_id"]
        )
        if p.get("supersedes"):
            old = self.evidence[p["supersedes"]]
            if old.status == EvidenceStatus.PENDING:
                old.status = EvidenceStatus.SUPERSEDED

    def _on_reviewer_registered(self, event: Event) -> None:
        p = event.payload
        self.reviewers[p["reviewer"]] = ReviewerState(
            domains=set(p["domains"]),
            conflict_students=set(p.get("conflict_students", [])),
            conflict_enterprises=set(p.get("conflict_enterprises", [])),
        )

    def _on_review_assigned(self, event: Event) -> None:
        self.evidence[event.payload["evidence_id"]].reviewer = event.payload["reviewer"]

    def _on_decision_recorded(self, event: Event) -> None:
        p = event.payload
        self.decisions[p["decision_id"]] = DecisionState(
            decision_id=p["decision_id"],
            evidence_id=p["evidence_id"],
            reviewer=p["reviewer"],
            decision=p["decision"],
            reason=p["reason"],
            recorded_on=date.fromisoformat(p["recorded_on"]),
        )
        self.decision_by_evidence[p["evidence_id"]] = p["decision_id"]
        evidence = self.evidence[p["evidence_id"]]
        if p["decision"] == Decision.RECOGNIZE:
            evidence.status = EvidenceStatus.RECOGNIZED
        elif p["decision"] == Decision.REJECT:
            evidence.status = EvidenceStatus.REJECTED

    def _on_conclusion_issued(self, event: Event) -> None:
        p = event.payload
        self.conclusions[p["conclusion_id"]] = ConclusionState(
            conclusion_id=p["conclusion_id"],
            evidence_id=p["evidence_id"],
            student_id=p["student"],
            skill=p["skill"],
            standard_id=p["standard_id"],
            standard_version=p["standard_version"],
            credits=p["credits"],
        )

    def _set_conclusion_status(self, event: Event, status: str) -> None:
        self.conclusions[event.payload["conclusion_id"]].status = status

    def _on_conclusion_suspended(self, event: Event) -> None:
        self._set_conclusion_status(event, ConclusionStatus.SUSPENDED)

    def _on_conclusion_restored(self, event: Event) -> None:
        self._set_conclusion_status(event, ConclusionStatus.ACTIVE)

    def _on_conclusion_replaced(self, event: Event) -> None:
        self._set_conclusion_status(event, ConclusionStatus.REPLACED)

    def _on_credential_issued(self, event: Event) -> None:
        p = event.payload
        self.credentials[p["credential_id"]] = {
            "conclusion_id": p["conclusion_id"],
            "student": p["student"],
            "skill": p["skill"],
            "digest": p["digest"],
        }
        self.credential_by_digest[p["digest"]] = p["credential_id"]

    def _on_student_updated(self, event: Event) -> None:
        p = event.payload
        student = self.students[p["student"]]
        student.status = p["status"]
        student.program = p["program"]

    def _on_transition_completed(self, event: Event) -> None:
        p = event.payload
        student = self.students[p["student"]]
        student.stage = p["to_stage"]
        student.transitions.append(
            {
                "from": p["from_stage"],
                "to": p["to_stage"],
                "batch": p["batch_id"],
                "on": event.occurred_on.isoformat(),
            }
        )

    def _on_posting_opened(self, event: Event) -> None:
        p = event.payload
        self.postings[p["posting"]] = PostingState(
            enterprise=p["enterprise"], skill=p["skill"], capacity=p["capacity"]
        )

    def _on_slot_allocated(self, event: Event) -> None:
        p = event.payload
        self.postings[p["posting"]].holders[p["student"]] = (
            event.seq,
            date.fromisoformat(p["requested_on"]),
        )
        # 一人一名额：到岗后从所有候补名单移除
        for posting in self.postings.values():
            posting.waitlist = [e for e in posting.waitlist if e.student_id != p["student"]]

    def _on_slot_waitlisted(self, event: Event) -> None:
        p = event.payload
        self.postings[p["posting"]].waitlist.append(
            WaitEntry(
                student_id=p["student"],
                requested_on=date.fromisoformat(p["requested_on"]),
                rank=p["rank"],
                seq=event.seq,
            )
        )

    def _on_slot_released(self, event: Event) -> None:
        p = event.payload
        self.postings[p["posting"]].holders.pop(p["student"], None)

    def _on_waitlist_removed(self, event: Event) -> None:
        p = event.payload
        posting = self.postings[p["posting"]]
        posting.waitlist = [e for e in posting.waitlist if e.student_id != p["student"]]

    def _on_posting_withdrawn(self, event: Event) -> None:
        self.postings[event.payload["posting"]].open = False

    def _on_disclosure_granted(self, event: Event) -> None:
        p = event.payload
        self.disclosures.setdefault((p["student"], p["enterprise"]), set()).update(p["scopes"])

    def _on_disclosure_revoked(self, event: Event) -> None:
        p = event.payload
        key = (p["student"], p["enterprise"])
        self.disclosures.setdefault(key, set()).difference_update(p["scopes"])


def build_projection(events: Iterable[Event]) -> Projection:
    projection = Projection()
    for event in events:
        projection.apply(event)
    return projection
