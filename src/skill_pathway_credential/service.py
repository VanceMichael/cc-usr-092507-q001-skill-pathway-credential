"""培养承接服务：能力证据、学分折算、实习岗位与授权查询的统一入口。

守住的核心不变量：
- 学分折算守恒：同一学生同一技能最多一条有效结论，学籍变动不复制学分；
- 岗位容量守恒：在岗人数不超过容量，一名学生最多占用一个名额；
- 结论依据固定：已认定的结论永久引用认定时的标准版本，换版只重算未完成计划；
- 授权最小披露：企业视图只包含学生明确授权的范围。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from .domain import (
    ALL_SCOPES,
    DECISIONS,
    SCOPE_COMPETENCY,
    SCOPE_CREDENTIALS,
    SCOPE_PATHWAY,
    STAGE_ORDER,
    BatchTransitionError,
    ConclusionStatus,
    Decision,
    DomainError,
    EvidenceStatus,
    ItemStatus,
    StudentStatus,
    SystemClock,
)
from .events import Event, EventStore
from .projection import Projection, build_projection


@dataclass
class Submission:
    """成果提交结果：相同内容直接返回原凭证，不产生新事件。"""

    outcome: str  # "existing" | "submitted"
    evidence_id: str
    status: str
    credential_id: str | None = None


class CredentialService:
    """贯通培养承接后端。所有命令在锁内完成"校验—写事件—折叠投影"，
    批量转段、岗位撤回与服务中断并发时仍守住学分与容量。"""

    def __init__(self, store: EventStore, clock=None, review_sla_days: int = 7):
        self._store = store
        self._clock = clock or SystemClock()
        self._review_sla = timedelta(days=review_sla_days)
        self._lock = threading.RLock()
        self._proj = build_projection(store.events())

    @classmethod
    def open(
        cls, path: str | Path | None = None, clock=None, review_sla_days: int = 7
    ) -> "CredentialService":
        """打开（或恢复）服务：从事件日志重放全部历史，中断后可继续到期复核。"""
        return cls(EventStore(Path(path)) if path else EventStore(), clock, review_sla_days)

    # ---- 内部工具 ----
    def _today(self) -> date:
        return self._clock.today()

    def _emit(self, kind: str, payload: dict[str, Any]) -> Event:
        event = self._store.append(kind, payload, self._today())
        self._proj.apply(event)
        return event

    def _require_student(self, student_id: str):
        student = self._proj.students.get(student_id)
        if student is None:
            raise DomainError(f"学生不存在：{student_id}")
        return student

    def _require_active(self, student_id: str):
        student = self._require_student(student_id)
        if student.status != StudentStatus.ACTIVE:
            raise DomainError("学生处于休学等非在籍状态，暂不可办理该业务")
        return student

    def _current_plan_item(self, student_id: str, skill: str):
        student = self._require_student(student_id)
        plan = self._proj.plans.get((student_id, student.stage))
        item = plan.items.get(skill) if plan else None
        if item is None:
            raise DomainError(f"技能 {skill} 不在学生当前阶段的培养计划中")
        return student, plan, item

    def _projection_as_of(self, on_date: date) -> Projection:
        return build_projection(self._store.events(upto=on_date))

    # ---- 标准与培养计划 ----
    def publish_standard(
        self, standard_id: str, credits: dict[str, int], effective_on: date | None = None
    ) -> int:
        """发布标准新版本。已取得的能力结论仍引用原依据；
        未完成的计划项按新版本重新计算所需学分。"""
        with self._lock:
            version = self._proj.standard_current.get(standard_id, 0) + 1
            self._emit(
                "standard_published",
                {
                    "standard_id": standard_id,
                    "version": version,
                    "credits": dict(credits),
                    "effective_on": (effective_on or self._today()).isoformat(),
                },
            )
            for (student_id, stage), plan in list(self._proj.plans.items()):
                for item in plan.items.values():
                    if item.standard_id != standard_id or item.status != ItemStatus.PENDING:
                        continue
                    if item.skill not in credits:
                        continue
                    if (
                        credits[item.skill] != item.required_credits
                        or item.standard_version != version
                    ):
                        self._emit(
                            "plan_item_recomputed",
                            {
                                "student": student_id,
                                "stage": stage,
                                "skill": item.skill,
                                "standard_version": version,
                                "required_credits": credits[item.skill],
                            },
                        )
                        self._refresh_item(student_id, stage, item.skill)
            return version

    def create_plan(
        self,
        student_id: str,
        program: str,
        stage: str,
        items: Iterable[tuple[str, str]],
    ) -> None:
        """建立某学段培养计划；items 为 (技能, 标准ID)，所需学分取当前标准版本。"""
        with self._lock:
            student = self._proj.students.get(student_id)
            if student is not None and student.stage != stage:
                raise DomainError("只能为学生当前学段建立培养计划")
            if (student_id, stage) in self._proj.plans:
                raise DomainError("该学段培养计划已存在")
            plan_items = []
            for skill, standard_id in items:
                version = self._proj.standard_current.get(standard_id)
                if version is None:
                    raise DomainError(f"标准不存在：{standard_id}")
                standard_credits = self._proj.standards[standard_id][version].credits
                if skill not in standard_credits:
                    raise DomainError(f"标准 {standard_id} 未覆盖技能 {skill}")
                plan_items.append(
                    {
                        "skill": skill,
                        "standard_id": standard_id,
                        "standard_version": version,
                        "required_credits": standard_credits[skill],
                    }
                )
            self._emit(
                "plan_created",
                {
                    "student": student_id,
                    "program": program,
                    "stage": stage,
                    "items": plan_items,
                },
            )

    # ---- 复核人 ----
    def register_reviewer(
        self,
        reviewer_id: str,
        domains: Iterable[str],
        conflict_students: Iterable[str] = (),
        conflict_enterprises: Iterable[str] = (),
    ) -> None:
        """登记复核人：可复核的技能范围，以及须回避的学生与企业。"""
        with self._lock:
            self._emit(
                "reviewer_registered",
                {
                    "reviewer": reviewer_id,
                    "domains": sorted(domains),
                    "conflict_students": sorted(conflict_students),
                    "conflict_enterprises": sorted(conflict_enterprises),
                },
            )

    def _eligible_reviewers(
        self, student_id: str, skill: str, enterprise: str | None
    ) -> list[str]:
        result = []
        for reviewer_id, reviewer in self._proj.reviewers.items():
            if skill not in reviewer.domains:
                continue
            if student_id in reviewer.conflict_students:
                continue
            if enterprise is not None and enterprise in reviewer.conflict_enterprises:
                continue
            result.append(reviewer_id)
        return result

    def _pick_reviewer(
        self, student_id: str, skill: str, enterprise: str | None
    ) -> str | None:
        candidates = self._eligible_reviewers(student_id, skill, enterprise)
        if not candidates:
            return None
        # 负载最小者优先，编号次序兜底，保证结果确定
        return min(candidates, key=lambda r: (self._proj.open_assignments(r), r))

    # ---- 成果提交与复核 ----
    def submit_evidence(
        self,
        student_id: str,
        skill: str,
        content: str,
        source_kind: str = "course",
        enterprise: str | None = None,
    ) -> Submission:
        """提交成果。同一成果再次提交返回原凭证（不产生新事件）；
        内容变化则暂停旧结论折算，等待新成果核查。"""
        with self._lock:
            self._require_active(student_id)
            self._current_plan_item(student_id, skill)
            digest = hashlib.sha256(
                f"{student_id}|{skill}|{content}".encode("utf-8")
            ).hexdigest()
            existing_id = self._proj.evidence_by_digest.get(digest)
            if existing_id is not None:
                existing = self._proj.evidence[existing_id]
                return Submission(
                    "existing",
                    existing_id,
                    existing.status,
                    self._proj.credential_by_digest.get(digest),
                )
            history = self._proj.evidence_by_skill.get((student_id, skill), [])
            supersedes = history[-1] if history else None
            evidence_id = f"EV-{digest[:12]}"
            today = self._today()
            self._emit(
                "evidence_submitted",
                {
                    "evidence_id": evidence_id,
                    "student": student_id,
                    "skill": skill,
                    "digest": digest,
                    "source_kind": source_kind,
                    "enterprise": enterprise,
                    "submitted_on": today.isoformat(),
                    "due_on": (today + self._review_sla).isoformat(),
                    "supersedes": supersedes,
                },
            )
            if supersedes is not None:
                for conclusion in self._proj.conclusions_for(student_id, skill):
                    if conclusion.status == ConclusionStatus.ACTIVE:
                        self._emit(
                            "conclusion_suspended",
                            {
                                "conclusion_id": conclusion.conclusion_id,
                                "reason": "成果内容变化，暂停折算等待核查",
                            },
                        )
            reviewer = self._pick_reviewer(student_id, skill, enterprise)
            if reviewer is not None:
                self._emit(
                    "review_assigned",
                    {"evidence_id": evidence_id, "reviewer": reviewer},
                )
            return Submission("submitted", evidence_id, EvidenceStatus.PENDING)

    def assign_review(self, evidence_id: str) -> str:
        """为待复核成果指派具备资格且无利益冲突的复核人。"""
        with self._lock:
            evidence = self._proj.evidence.get(evidence_id)
            if evidence is None:
                raise DomainError(f"成果不存在：{evidence_id}")
            if evidence.status != EvidenceStatus.PENDING:
                raise DomainError("成果当前状态无需复核")
            if evidence.reviewer is not None:
                return evidence.reviewer
            reviewer = self._pick_reviewer(
                evidence.student_id, evidence.skill, evidence.enterprise
            )
            if reviewer is None:
                raise DomainError("暂无具备资格且无利益冲突的复核人")
            self._emit(
                "review_assigned", {"evidence_id": evidence_id, "reviewer": reviewer}
            )
            return reviewer

    def record_decision(
        self, evidence_id: str, reviewer_id: str, decision: str, reason: str
    ) -> str:
        """记录承认/拒绝/补证决定。承认时按认定时有效的标准版本折算学分；
        拒绝时恢复被暂停的旧结论；补证时成果保持待复核。"""
        with self._lock:
            if decision not in DECISIONS:
                raise DomainError(f"未知决定类型：{decision}")
            evidence = self._proj.evidence.get(evidence_id)
            if evidence is None:
                raise DomainError(f"成果不存在：{evidence_id}")
            if evidence.status != EvidenceStatus.PENDING:
                raise DomainError("成果当前状态不可复核")
            if evidence.reviewer != reviewer_id:
                raise DomainError("该成果未指派给此复核人")
            if reviewer_id not in self._eligible_reviewers(
                evidence.student_id, evidence.skill, evidence.enterprise
            ):
                raise DomainError("复核人不具备资格或存在利益冲突")
            decision_id = f"DC-{self._store.next_seq()}"
            self._emit(
                "decision_recorded",
                {
                    "decision_id": decision_id,
                    "evidence_id": evidence_id,
                    "reviewer": reviewer_id,
                    "decision": decision,
                    "reason": reason,
                    "recorded_on": self._today().isoformat(),
                },
            )
            if decision == Decision.RECOGNIZE:
                self._recognize(evidence)
            elif decision == Decision.REJECT:
                for conclusion in self._proj.conclusions_for(
                    evidence.student_id, evidence.skill
                ):
                    if conclusion.status == ConclusionStatus.SUSPENDED:
                        self._emit(
                            "conclusion_restored",
                            {"conclusion_id": conclusion.conclusion_id},
                        )
            return decision_id

    def _recognize(self, evidence) -> None:
        student = self._proj.students[evidence.student_id]
        _, _, item = self._current_plan_item(evidence.student_id, evidence.skill)
        version = self._proj.standard_current[item.standard_id]
        credits = self._proj.standards[item.standard_id][version].credits[evidence.skill]
        conclusion_id = f"CN-{self._store.next_seq()}"
        self._emit(
            "conclusion_issued",
            {
                "conclusion_id": conclusion_id,
                "evidence_id": evidence.evidence_id,
                "student": evidence.student_id,
                "skill": evidence.skill,
                "standard_id": item.standard_id,
                "standard_version": version,
                "credits": credits,
            },
        )
        credential_id = f"CRD-{evidence.digest[:16]}"
        self._emit(
            "credential_issued",
            {
                "credential_id": credential_id,
                "conclusion_id": conclusion_id,
                "student": evidence.student_id,
                "skill": evidence.skill,
                "digest": evidence.digest,
            },
        )
        for old in self._proj.conclusions_for(evidence.student_id, evidence.skill):
            if (
                old.conclusion_id != conclusion_id
                and old.status == ConclusionStatus.SUSPENDED
            ):
                self._emit(
                    "conclusion_replaced",
                    {"conclusion_id": old.conclusion_id, "by": conclusion_id},
                )
        self._refresh_item(evidence.student_id, student.stage, evidence.skill)

    def _refresh_item(self, student_id: str, stage: str, skill: str) -> None:
        plan = self._proj.plans.get((student_id, stage))
        if plan is None:
            return
        item = plan.items.get(skill)
        if item is None or item.status != ItemStatus.PENDING:
            return
        achieved = sum(
            c.credits
            for c in self._proj.conclusions_for(student_id, skill)
            if c.status == ConclusionStatus.ACTIVE
        )
        if achieved >= item.required_credits:
            self._emit(
                "plan_item_completed",
                {"student": student_id, "stage": stage, "skill": skill},
            )

    # ---- 学籍变动：转学、休学、专业调整 ----
    def _release_slot_if_holder(self, student_id: str, reason: str) -> None:
        posting_id = self._proj.holding_posting(student_id)
        if posting_id is not None:
            self._emit(
                "slot_released",
                {"student": student_id, "posting": posting_id, "reason": reason},
            )
            self._promote(posting_id)

    def _update_student(
        self, student_id: str, status: str, program: str, change: str
    ) -> None:
        self._require_student(student_id)
        self._release_slot_if_holder(student_id, f"学籍变动：{change}")
        self._emit(
            "student_updated",
            {
                "student": student_id,
                "status": status,
                "program": program,
                "change": change,
            },
        )

    def transfer(self, student_id: str, new_program: str) -> None:
        """转学：实习名额释放，学分随人走，不重复占用。"""
        with self._lock:
            self._update_student(student_id, StudentStatus.ACTIVE, new_program, "转学")

    def change_major(self, student_id: str, new_program: str) -> None:
        """专业调整：名额释放，已折算学分保持不变。"""
        with self._lock:
            self._update_student(student_id, StudentStatus.ACTIVE, new_program, "专业调整")

    def suspend(self, student_id: str) -> None:
        """休学：名额释放，在办业务暂停，学分保留。"""
        with self._lock:
            student = self._require_student(student_id)
            self._update_student(
                student_id, StudentStatus.SUSPENDED, student.program, "休学"
            )

    def resume(self, student_id: str) -> None:
        """复学。"""
        with self._lock:
            student = self._require_student(student_id)
            if student.status != StudentStatus.SUSPENDED:
                raise DomainError("学生未处于休学状态")
            self._update_student(student_id, StudentStatus.ACTIVE, student.program, "复学")

    # ---- 实习岗位与容量 ----
    def open_posting(
        self, posting_id: str, enterprise: str, skill: str, capacity: int
    ) -> None:
        with self._lock:
            if capacity < 1:
                raise DomainError("岗位容量至少为 1")
            if posting_id in self._proj.postings:
                raise DomainError(f"岗位已存在：{posting_id}")
            self._emit(
                "posting_opened",
                {
                    "posting": posting_id,
                    "enterprise": enterprise,
                    "skill": skill,
                    "capacity": capacity,
                },
            )

    def apply_for_slot(self, student_id: str, posting_id: str) -> str:
        """申请岗位，返回 allocated / waitlisted。一名学生最多占一个名额。"""
        with self._lock:
            self._require_active(student_id)
            posting = self._proj.postings.get(posting_id)
            if posting is None:
                raise DomainError(f"岗位不存在：{posting_id}")
            if not posting.open:
                raise DomainError("岗位已撤回")
            if self._proj.holding_posting(student_id) is not None:
                raise DomainError("学生已占用一个实习名额")
            if any(e.student_id == student_id for e in posting.waitlist):
                return "waitlisted"
            today = self._today().isoformat()
            if posting.free_capacity > 0:
                self._emit(
                    "slot_allocated",
                    {
                        "student": student_id,
                        "posting": posting_id,
                        "requested_on": today,
                        "rank": 1,
                    },
                )
                return "allocated"
            self._emit(
                "slot_waitlisted",
                {
                    "student": student_id,
                    "posting": posting_id,
                    "requested_on": today,
                    "rank": 1,
                },
            )
            return "waitlisted"

    def release_slot(
        self, student_id: str, posting_id: str, reason: str = "学生主动退出"
    ) -> None:
        with self._lock:
            posting = self._proj.postings.get(posting_id)
            if posting is None or student_id not in posting.holders:
                raise DomainError("学生未占用该岗位")
            self._emit(
                "slot_released",
                {"student": student_id, "posting": posting_id, "reason": reason},
            )
            self._promote(posting_id)

    def _promote(self, posting_id: str) -> None:
        """按候补顺序递补；休学或已占用名额的学生让位（不重复占用容量）。"""
        posting = self._proj.postings[posting_id]
        while posting.open and posting.free_capacity > 0:
            ordered = posting.ordered_waitlist()
            if not ordered:
                return
            entry = ordered[0]
            student = self._proj.students.get(entry.student_id)
            if (
                student is None
                or student.status != StudentStatus.ACTIVE
                or self._proj.holding_posting(entry.student_id) is not None
            ):
                self._emit(
                    "waitlist_removed",
                    {"student": entry.student_id, "posting": posting_id},
                )
                continue
            self._emit(
                "slot_allocated",
                {
                    "student": entry.student_id,
                    "posting": posting_id,
                    "requested_on": entry.requested_on.isoformat(),
                    "rank": entry.rank,
                },
            )

    def withdraw_posting(self, posting_id: str) -> None:
        """企业临时撤回岗位：在岗学生按原占用顺序、候补按原申请顺序，
        以优先候补身份安置到同技能的空缺岗位。"""
        with self._lock:
            posting = self._proj.postings.get(posting_id)
            if posting is None:
                raise DomainError(f"岗位不存在：{posting_id}")
            if not posting.open:
                raise DomainError("岗位已撤回")
            self._emit("posting_withdrawn", {"posting": posting_id})
            displaced = sorted(posting.holders.items(), key=lambda kv: kv[1][0])
            queued = posting.ordered_waitlist()
            for student_id, _ in displaced:
                self._emit(
                    "slot_released",
                    {
                        "student": student_id,
                        "posting": posting_id,
                        "reason": "企业撤回岗位",
                    },
                )
            for entry in queued:
                self._emit(
                    "waitlist_removed",
                    {"student": entry.student_id, "posting": posting_id},
                )
            affected = [(sid, req) for sid, (_, req) in displaced] + [
                (e.student_id, e.requested_on) for e in queued
            ]
            touched: set[str] = set()
            for student_id, requested_on in affected:
                student = self._proj.students.get(student_id)
                if student is None or student.status != StudentStatus.ACTIVE:
                    continue
                if self._proj.holding_posting(student_id) is not None:
                    continue
                alternatives = sorted(
                    (
                        (pid, p)
                        for pid, p in self._proj.postings.items()
                        if pid != posting_id and p.open and p.skill == posting.skill
                    ),
                    key=lambda kv: (-kv[1].free_capacity, kv[0]),
                )
                if not alternatives:
                    continue
                target_id, target = alternatives[0]
                kind = (
                    "slot_allocated" if target.free_capacity > 0 else "slot_waitlisted"
                )
                self._emit(
                    kind,
                    {
                        "student": student_id,
                        "posting": target_id,
                        "requested_on": requested_on.isoformat(),
                        "rank": 0,
                    },
                )
                touched.add(target_id)
            for pid in touched:
                self._promote(pid)

    # ---- 批量转段 ----
    def batch_transition(self, student_ids: Iterable[str], to_stage: str) -> str:
        """批量转段：任一学生不合格则整体失败，不产生部分事件。"""
        with self._lock:
            if to_stage not in STAGE_ORDER:
                raise DomainError(f"未知学段：{to_stage}")
            target_idx = STAGE_ORDER.index(to_stage)
            if target_idx == 0:
                raise DomainError("转段目标不能是中职")
            errors: dict[str, list[str]] = {}
            for student_id in student_ids:
                problems: list[str] = []
                student = self._proj.students.get(student_id)
                if student is None:
                    problems.append("学生不存在")
                else:
                    if student.status != StudentStatus.ACTIVE:
                        problems.append("学生非在籍状态")
                    if student.stage != STAGE_ORDER[target_idx - 1]:
                        problems.append(f"当前学段为 {student.stage}，不满足转段前提")
                    else:
                        plan = self._proj.plans.get((student_id, student.stage))
                        if plan is None:
                            problems.append("缺少当前学段培养计划")
                        else:
                            for item in plan.items.values():
                                if item.status != ItemStatus.COMPLETED:
                                    problems.append(
                                        f"技能 {item.skill} 的培养计划未完成"
                                    )
                if problems:
                    errors[student_id] = problems
            if errors:
                raise BatchTransitionError(errors)
            batch_id = f"BT-{self._store.next_seq()}"
            events = [
                (
                    "transition_completed",
                    {
                        "student": student_id,
                        "from_stage": self._proj.students[student_id].stage,
                        "to_stage": to_stage,
                        "batch_id": batch_id,
                    },
                )
                for student_id in student_ids
            ]
            for event in self._store.append_many(events, self._today()):
                self._proj.apply(event)
            return batch_id

    # ---- 授权与最小披露 ----
    def grant_disclosure(
        self, student_id: str, enterprise: str, scopes: Iterable[str]
    ) -> None:
        """学生授权企业查看指定范围的能力摘要。"""
        with self._lock:
            self._require_student(student_id)
            unknown = set(scopes) - ALL_SCOPES
            if unknown:
                raise DomainError(f"未知披露范围：{sorted(unknown)}")
            self._emit(
                "disclosure_granted",
                {
                    "student": student_id,
                    "enterprise": enterprise,
                    "scopes": sorted(scopes),
                },
            )

    def revoke_disclosure(
        self, student_id: str, enterprise: str, scopes: Iterable[str]
    ) -> None:
        with self._lock:
            self._require_student(student_id)
            self._emit(
                "disclosure_revoked",
                {
                    "student": student_id,
                    "enterprise": enterprise,
                    "scopes": sorted(set(scopes) & ALL_SCOPES),
                },
            )

    def enterprise_view(
        self, student_id: str, enterprise: str, on_date: date | None = None
    ) -> dict:
        """企业视图：只包含学生在指定日期前明确授权的范围。"""
        proj = self._projection_as_of(on_date) if on_date else self._proj
        scopes = proj.disclosures.get((student_id, enterprise), set())
        view: dict[str, Any] = {
            "student": student_id,
            "enterprise": enterprise,
            "granted_scopes": sorted(scopes),
        }
        if SCOPE_COMPETENCY in scopes:
            view["competency_summary"] = [
                {
                    "skill": c.skill,
                    "credits": c.credits,
                    "standard_id": c.standard_id,
                    "standard_version": c.standard_version,
                }
                for c in proj.conclusions.values()
                if c.student_id == student_id and c.status == ConclusionStatus.ACTIVE
            ]
        if SCOPE_CREDENTIALS in scopes:
            view["credentials"] = [
                {"credential_id": cid, "skill": c["skill"]}
                for cid, c in proj.credentials.items()
                if c["student"] == student_id
            ]
        if SCOPE_PATHWAY in scopes:
            student = proj.students.get(student_id)
            if student is not None:
                view["pathway"] = {
                    "program": student.program,
                    "stage": student.stage,
                    "transitions": list(student.transitions),
                }
        return view

    # ---- 查询与解释 ----
    def events(self, upto: date | None = None) -> list[Event]:
        return self._store.events(upto)

    def student_info(self, student_id: str) -> dict:
        student = self._require_student(student_id)
        return {
            "program": student.program,
            "stage": student.stage,
            "status": student.status,
        }

    def posting_state(self, posting_id: str) -> dict:
        posting = self._proj.postings.get(posting_id)
        if posting is None:
            raise DomainError(f"岗位不存在：{posting_id}")
        return {
            "open": posting.open,
            "capacity": posting.capacity,
            "holders": sorted(posting.holders),
            "waitlist": [e.student_id for e in posting.ordered_waitlist()],
        }

    def credit_balance(self, student_id: str, on_date: date | None = None) -> dict[str, int]:
        """有效结论折算的学分余额。"""
        proj = self._projection_as_of(on_date) if on_date else self._proj
        balance: dict[str, int] = {}
        for c in proj.conclusions.values():
            if c.student_id == student_id and c.status == ConclusionStatus.ACTIVE:
                balance[c.skill] = balance.get(c.skill, 0) + c.credits
        return balance

    def skill_basis(self, student_id: str, skill: str) -> dict:
        """回答教务处的核心问题：该技能由谁认定、按哪版标准折算。"""
        conclusion = self._proj.active_conclusion(student_id, skill)
        if conclusion is None:
            raise DomainError("该技能暂无有效能力结论")
        decision = self._proj.decisions.get(
            self._proj.decision_by_evidence.get(conclusion.evidence_id, "")
        )
        evidence = self._proj.evidence[conclusion.evidence_id]
        return {
            "student": student_id,
            "skill": skill,
            "conclusion_id": conclusion.conclusion_id,
            "standard_id": conclusion.standard_id,
            "standard_version": conclusion.standard_version,
            "credits": conclusion.credits,
            "reviewer": decision.reviewer if decision else None,
            "decision_id": decision.decision_id if decision else None,
            "credential_id": self._proj.credential_by_digest.get(evidence.digest),
        }

    def explain_decision(self, decision_id: str) -> dict:
        """向学生或企业解释一次承认/拒绝/补证决定的依据。"""
        decision = self._proj.decisions.get(decision_id)
        if decision is None:
            raise DomainError(f"决定不存在：{decision_id}")
        evidence = self._proj.evidence[decision.evidence_id]
        reviewer = self._proj.reviewers[decision.reviewer]
        rules = ["复核人具备该技能复核资格", "复核人与学生及涉事企业无利益冲突"]
        basis = None
        credential_id = None
        if decision.decision == Decision.RECOGNIZE:
            conclusion = next(
                c
                for c in self._proj.conclusions.values()
                if c.evidence_id == evidence.evidence_id
            )
            basis = {
                "standard_id": conclusion.standard_id,
                "standard_version": conclusion.standard_version,
                "credits": conclusion.credits,
            }
            rules.append("按认定时有效的标准版本折算，结论永久引用该版本")
            credential_id = self._proj.credential_by_digest.get(evidence.digest)
        elif decision.decision == Decision.REJECT:
            rules.append("如存在被暂停的旧结论，拒绝后原结论恢复折算")
        elif decision.decision == Decision.SUPPLEMENT:
            rules.append("要求补充证据，成果保持待复核状态")
        return {
            "decision_id": decision_id,
            "decision": decision.decision,
            "reason": decision.reason,
            "reviewer": decision.reviewer,
            "reviewer_domains": sorted(reviewer.domains),
            "student": evidence.student_id,
            "skill": evidence.skill,
            "evidence_id": evidence.evidence_id,
            "standard_basis": basis,
            "credential_id": credential_id,
            "rules": rules,
            "recorded_on": decision.recorded_on.isoformat(),
        }

    def pathway_as_of(self, student_id: str, on_date: date) -> dict:
        """还原学生在指定日期的完整培养路径。"""
        proj = self._projection_as_of(on_date)
        student = proj.students.get(student_id)
        if student is None:
            raise DomainError(f"学生不存在：{student_id}")
        plans = {}
        for (sid, stage), plan in proj.plans.items():
            if sid != student_id:
                continue
            plans[stage] = {
                skill: {
                    "required_credits": item.required_credits,
                    "standard_version": item.standard_version,
                    "status": item.status,
                }
                for skill, item in plan.items.items()
            }
        return {
            "student": student_id,
            "as_of": on_date.isoformat(),
            "program": student.program,
            "stage": student.stage,
            "status": student.status,
            "transitions": list(student.transitions),
            "plans": plans,
            "conclusions": [
                {
                    "conclusion_id": c.conclusion_id,
                    "skill": c.skill,
                    "standard_id": c.standard_id,
                    "standard_version": c.standard_version,
                    "credits": c.credits,
                    "status": c.status,
                }
                for c in proj.conclusions.values()
                if c.student_id == student_id
            ],
            "credits": self.credit_balance(student_id, on_date),
        }

    def authorization_as_of(self, student_id: str, on_date: date) -> dict[str, list[str]]:
        """还原学生在指定日期对各企业的授权状态。"""
        proj = self._projection_as_of(on_date)
        return {
            enterprise: sorted(scopes)
            for (sid, enterprise), scopes in proj.disclosures.items()
            if sid == student_id and scopes
        }

    def pending_reviews(self) -> list[dict]:
        """全部待复核成果（含未到期）。"""
        return self._review_list(self._today(), include_future=True)

    def due_reviews(self, on_date: date | None = None) -> list[dict]:
        """到期应复核的成果；服务恢复后据此继续复核。"""
        return self._review_list(on_date or self._today(), include_future=False)

    def _review_list(self, on: date, include_future: bool) -> list[dict]:
        pending = [
            e
            for e in self._proj.evidence.values()
            if e.status == EvidenceStatus.PENDING
            and (include_future or e.due_on <= on)
        ]
        return [
            {
                "evidence_id": e.evidence_id,
                "student": e.student_id,
                "skill": e.skill,
                "due_on": e.due_on.isoformat(),
                "reviewer": e.reviewer,
            }
            for e in sorted(pending, key=lambda e: (e.due_on, e.evidence_id))
        ]

    def verify_invariants(self) -> list[str]:
        """守恒检查：学分不重复、岗位不超容、名额不重复占用。返回空列表表示守恒。"""
        problems: list[str] = []
        seen: dict[tuple[str, str], str] = {}
        for c in self._proj.conclusions.values():
            if c.status != ConclusionStatus.ACTIVE:
                continue
            key = (c.student_id, c.skill)
            if key in seen:
                problems.append(f"学生 {c.student_id} 技能 {c.skill} 存在重复有效结论")
            seen[key] = c.conclusion_id
        for posting_id, posting in self._proj.postings.items():
            if len(posting.holders) > posting.capacity:
                problems.append(f"岗位 {posting_id} 超出容量")
            waiting = [e.student_id for e in posting.waitlist]
            if len(waiting) != len(set(waiting)):
                problems.append(f"岗位 {posting_id} 候补名单重复")
            for student_id in posting.holders:
                if student_id in waiting:
                    problems.append(f"学生 {student_id} 同时在岗与候补")
        hold_count: dict[str, int] = {}
        for posting in self._proj.postings.values():
            for student_id in posting.holders:
                hold_count[student_id] = hold_count.get(student_id, 0) + 1
        for student_id, count in hold_count.items():
            if count > 1:
                problems.append(f"学生 {student_id} 重复占用 {count} 个实习名额")
        return problems
