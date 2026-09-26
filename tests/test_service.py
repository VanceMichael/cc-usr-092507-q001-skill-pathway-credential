import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_pathway_credential.domain import (
    SCOPE_COMPETENCY,
    SCOPE_CREDENTIALS,
    BatchTransitionError,
    Decision,
    DomainError,
    FixedClock,
    ItemStatus,
    Stage,
    StudentStatus,
)
from skill_pathway_credential.service import CredentialService

DAY1 = date(2026, 9, 1)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(DAY1)
        self.service = CredentialService.open(clock=self.clock)
        self.service.publish_standard("STD-WELD", {"焊接": 3})
        self.service.register_reviewer("R1", {"焊接"})

    def make_student(self, student_id, skills=("焊接",)):
        self.service.create_plan(
            student_id, "智能制造", Stage.SECONDARY, [(s, "STD-WELD") for s in skills]
        )

    def recognize(self, student_id, skill, content, reviewer="R1"):
        submission = self.service.submit_evidence(student_id, skill, content)
        decision_id = self.service.record_decision(
            submission.evidence_id, reviewer, Decision.RECOGNIZE, "证据充分"
        )
        return submission, decision_id


class EvidenceIdempotencyTest(ServiceTestBase):
    def test_duplicate_submission_returns_original_credential(self):
        self.make_student("S1")
        self.recognize("S1", "焊接", "作品记录-v1")
        credential = self.service.skill_basis("S1", "焊接")["credential_id"]

        before = len(self.service.events())
        again = self.service.submit_evidence("S1", "焊接", "作品记录-v1")
        self.assertEqual(again.outcome, "existing")
        self.assertEqual(again.credential_id, credential)
        # 幂等：重复提交不产生任何新事件
        self.assertEqual(len(self.service.events()), before)

    def test_changed_content_suspends_conversion_until_review(self):
        self.make_student("S1")
        self.recognize("S1", "焊接", "作品记录-v1")
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})

        changed = self.service.submit_evidence("S1", "焊接", "作品记录-v2-有改动")
        self.assertEqual(changed.outcome, "submitted")
        # 内容变化：旧结论暂停折算，等待核查
        self.assertEqual(self.service.credit_balance("S1"), {})

        self.service.record_decision(
            changed.evidence_id, "R1", Decision.RECOGNIZE, "新成果核查通过"
        )
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})
        conclusions = self.service.pathway_as_of("S1", self.clock.today())["conclusions"]
        self.assertEqual(
            sorted(c["status"] for c in conclusions), ["active", "replaced"]
        )
        self.assertEqual(self.service.verify_invariants(), [])

    def test_rejected_replacement_restores_original_conclusion(self):
        self.make_student("S1")
        self.recognize("S1", "焊接", "作品记录-v1")
        changed = self.service.submit_evidence("S1", "焊接", "作品记录-v2-有改动")
        self.assertEqual(self.service.credit_balance("S1"), {})

        self.service.record_decision(
            changed.evidence_id, "R1", Decision.REJECT, "新成果不符合要求"
        )
        # 新成果被拒绝，原结论恢复折算
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})
        self.assertEqual(self.service.verify_invariants(), [])


class ReviewerEligibilityTest(ServiceTestBase):
    def test_reviewer_must_be_qualified_and_conflict_free(self):
        self.make_student("S1")
        self.service.register_reviewer("R2", {"数控"})  # 无焊接资格
        self.service.register_reviewer("R3", {"焊接"}, conflict_students={"S1"})  # 须回避
        self.service.register_reviewer("R4", {"焊接"}, conflict_enterprises={"ENT-1"})

        submission = self.service.submit_evidence(
            "S1", "焊接", "企业实训记录", source_kind="internship", enterprise="ENT-1"
        )
        # R2 无资格、R3 与学生冲突、R4 与涉事企业冲突 → 只能指派 R1
        pending = self.service.pending_reviews()
        self.assertEqual(pending[0]["reviewer"], "R1")

        # 未被指派的复核人不能记录决定
        with self.assertRaises(DomainError):
            self.service.record_decision(
                submission.evidence_id, "R2", Decision.RECOGNIZE, "越权认定"
            )

    def test_no_eligible_reviewer_leaves_evidence_unassigned(self):
        self.service.publish_standard("STD-CNC", {"数控": 2})
        self.service.create_plan("S2", "智能制造", Stage.SECONDARY, [("数控", "STD-CNC")])
        self.service.register_reviewer("R5", {"数控"}, conflict_students={"S2"})

        submission = self.service.submit_evidence("S2", "数控", "竞赛获奖证明", "competition")
        pending = {d["evidence_id"]: d for d in self.service.pending_reviews()}
        self.assertIsNone(pending[submission.evidence_id]["reviewer"])
        with self.assertRaisesRegex(DomainError, "无利益冲突"):
            self.service.assign_review(submission.evidence_id)


class StandardVersionTest(ServiceTestBase):
    def test_republish_pins_existing_conclusions_and_recomputes_pending(self):
        self.make_student("S1")
        self.make_student("S2")
        self.recognize("S1", "焊接", "作品记录-v1")  # 按 v1 折算 3 学分

        self.service.publish_standard("STD-WELD", {"焊接": 5})  # 标准换版

        # 已取得结论仍引用原依据
        basis = self.service.skill_basis("S1", "焊接")
        self.assertEqual((basis["standard_version"], basis["credits"]), (1, 3))
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})

        # 未完成计划按新版本重新计算
        plans = self.service.pathway_as_of("S2", self.clock.today())["plans"]
        item = plans[Stage.SECONDARY]["焊接"]
        self.assertEqual((item["required_credits"], item["standard_version"]), (5, 2))

        # 换版后认定的结论按新版本折算
        self.recognize("S2", "焊接", "作品记录-v1")
        basis2 = self.service.skill_basis("S2", "焊接")
        self.assertEqual((basis2["standard_version"], basis2["credits"]), (2, 5))
        self.assertEqual(self.service.verify_invariants(), [])


class StatusChangeConservationTest(ServiceTestBase):
    def test_transfer_suspend_major_change_preserve_credits_and_slots(self):
        self.make_student("S1")
        self.make_student("S2")
        self.recognize("S1", "焊接", "作品记录-v1")
        self.service.open_posting("P1", "ENT-1", "焊接", 1)
        self.assertEqual(self.service.apply_for_slot("S1", "P1"), "allocated")
        self.assertEqual(self.service.apply_for_slot("S2", "P1"), "waitlisted")

        # 转学：名额释放给候补，学分随人走
        self.service.transfer("S1", "装备制造")
        self.assertEqual(self.service.posting_state("P1")["holders"], ["S2"])
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})

        # 休学：在办业务暂停
        self.service.suspend("S1")
        with self.assertRaises(DomainError):
            self.service.submit_evidence("S1", "焊接", "作品记录-v2")
        with self.assertRaises(DomainError):
            self.service.apply_for_slot("S1", "P1")

        # 复学、专业调整：学分不重复、不丢失
        self.service.resume("S1")
        self.service.change_major("S1", "智能焊接")
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})
        self.assertEqual(self.service.student_info("S1")["program"], "智能焊接")
        self.assertEqual(self.service.verify_invariants(), [])


class SlotCapacityTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        for sid in ("S1", "S2", "S3", "S4"):
            self.make_student(sid)

    def test_waitlist_promoted_in_request_order(self):
        self.service.open_posting("P1", "ENT-1", "焊接", 1)
        self.assertEqual(self.service.apply_for_slot("S1", "P1"), "allocated")
        self.assertEqual(self.service.apply_for_slot("S2", "P1"), "waitlisted")
        self.service.release_slot("S1", "P1")
        self.assertEqual(self.service.posting_state("P1")["holders"], ["S2"])
        self.assertEqual(self.service.verify_invariants(), [])

    def test_withdrawal_reallocates_displaced_before_newcomers(self):
        self.service.open_posting("P1", "ENT-1", "数控", 1)  # 技能不同，不是安置去向
        self.service.open_posting("P2", "ENT-2", "焊接", 1)
        self.service.open_posting("P3", "ENT-3", "焊接", 1)
        self.service.apply_for_slot("S1", "P1")
        self.service.apply_for_slot("S2", "P2")
        self.service.apply_for_slot("S3", "P2")  # 候补

        # 企业临时撤回 P2：在岗的 S2 优先安置到 P3，候补 S3 转为 P3 优先候补
        self.service.withdraw_posting("P2")
        state = self.service.posting_state("P3")
        self.assertEqual(state["holders"], ["S2"])
        self.assertEqual(state["waitlist"], ["S3"])
        self.assertFalse(self.service.posting_state("P2")["open"])

        # 新申请人排在受影响学生之后
        self.assertEqual(self.service.apply_for_slot("S4", "P3"), "waitlisted")
        self.assertEqual(self.service.posting_state("P3")["waitlist"], ["S3", "S4"])
        self.assertEqual(self.service.verify_invariants(), [])

    def test_concurrent_applications_never_exceed_capacity(self):
        students = [f"ST{i}" for i in range(8)]
        for sid in students:
            self.make_student(sid)
        self.service.open_posting("P1", "ENT-1", "焊接", 3)

        results = []
        threads = [
            threading.Thread(
                target=lambda s=sid: results.append(self.service.apply_for_slot(s, "P1"))
            )
            for sid in students
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("allocated"), 3)
        self.assertEqual(results.count("waitlisted"), 5)
        self.assertEqual(len(self.service.posting_state("P1")["holders"]), 3)
        self.assertEqual(self.service.verify_invariants(), [])


class BatchTransitionTest(ServiceTestBase):
    def test_batch_transition_is_atomic(self):
        self.make_student("S1")
        self.make_student("S2")
        self.recognize("S1", "焊接", "作品记录-v1")

        # S2 计划未完成 → 整批失败，S1 不被部分转段
        with self.assertRaises(BatchTransitionError) as ctx:
            self.service.batch_transition(["S1", "S2"], Stage.HIGHER)
        self.assertIn("S2", ctx.exception.errors)
        self.assertEqual(self.service.student_info("S1")["stage"], Stage.SECONDARY)
        self.assertFalse(
            any(e.kind == "transition_completed" for e in self.service.events())
        )

        self.recognize("S2", "焊接", "作品记录-v1")
        batch_id = self.service.batch_transition(["S1", "S2"], Stage.HIGHER)
        for sid in ("S1", "S2"):
            info = self.service.student_info(sid)
            self.assertEqual(info["stage"], Stage.HIGHER)
            transitions = self.service.pathway_as_of(sid, self.clock.today())["transitions"]
            self.assertEqual(transitions[0]["batch"], batch_id)
        self.assertEqual(self.service.verify_invariants(), [])


class RecoveryTest(ServiceTestBase):
    def test_recovery_resumes_due_reviews_after_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            service = CredentialService.open(path, clock=self.clock)
            service.publish_standard("STD-WELD", {"焊接": 3})
            service.register_reviewer("R1", {"焊接"})
            service.create_plan("S1", "智能制造", Stage.SECONDARY, [("焊接", "STD-WELD")])
            submission = service.submit_evidence("S1", "焊接", "作品记录-v1")

            self.clock.advance(8)  # 超过 7 天复核期限，期间服务中断

            recovered = CredentialService.open(path, clock=self.clock)
            due = recovered.due_reviews()
            self.assertEqual([d["evidence_id"] for d in due], [submission.evidence_id])

            # 恢复后继续到期复核
            recovered.record_decision(
                submission.evidence_id, "R1", Decision.RECOGNIZE, "恢复后核查通过"
            )
            self.assertEqual(recovered.credit_balance("S1"), {"焊接": 3})

            # 再次重启，状态完整保留
            again = CredentialService.open(path, clock=self.clock)
            self.assertEqual(again.credit_balance("S1"), {"焊接": 3})
            self.assertEqual(again.due_reviews(), [])
            self.assertEqual(again.verify_invariants(), [])


class DisclosureTest(ServiceTestBase):
    def test_enterprise_view_is_minimal_and_consent_based(self):
        self.make_student("S1")
        self.recognize("S1", "焊接", "作品记录-v1")

        # 未授权：企业看不到任何能力数据
        view = self.service.enterprise_view("S1", "ENT-1")
        self.assertEqual(view["granted_scopes"], [])
        self.assertNotIn("competency_summary", view)
        self.assertNotIn("credentials", view)

        # 只授权能力摘要 → 看不到凭证清单
        self.service.grant_disclosure("S1", "ENT-1", {SCOPE_COMPETENCY})
        view = self.service.enterprise_view("S1", "ENT-1")
        self.assertEqual(
            view["competency_summary"],
            [
                {
                    "skill": "焊接",
                    "credits": 3,
                    "standard_id": "STD-WELD",
                    "standard_version": 1,
                }
            ],
        )
        self.assertNotIn("credentials", view)

        # 追加授权、再撤销
        self.service.grant_disclosure("S1", "ENT-1", {SCOPE_CREDENTIALS})
        self.assertEqual(len(self.service.enterprise_view("S1", "ENT-1")["credentials"]), 1)
        self.service.revoke_disclosure("S1", "ENT-1", {SCOPE_COMPETENCY})
        view = self.service.enterprise_view("S1", "ENT-1")
        self.assertNotIn("competency_summary", view)
        self.assertIn("credentials", view)

        # 其他企业不受影响
        self.assertEqual(
            self.service.enterprise_view("S1", "ENT-2")["granted_scopes"], []
        )


class ExplainabilityTest(ServiceTestBase):
    def test_explain_recognize_reject_and_supplement(self):
        self.make_student("S1")
        _, decision_id = self.recognize("S1", "焊接", "作品记录-v1")

        explanation = self.service.explain_decision(decision_id)
        self.assertEqual(explanation["decision"], Decision.RECOGNIZE)
        self.assertEqual(explanation["reviewer"], "R1")
        self.assertEqual(explanation["reason"], "证据充分")
        self.assertEqual(explanation["standard_basis"]["standard_version"], 1)
        self.assertIsNotNone(explanation["credential_id"])
        self.assertTrue(any("资格" in rule for rule in explanation["rules"]))
        self.assertTrue(any("利益冲突" in rule for rule in explanation["rules"]))

        # 补证：成果保持待复核，可继续办理
        changed = self.service.submit_evidence("S1", "焊接", "作品记录-v2")
        supplement_id = self.service.record_decision(
            changed.evidence_id, "R1", Decision.SUPPLEMENT, "缺少企业签章"
        )
        explanation = self.service.explain_decision(supplement_id)
        self.assertEqual(explanation["decision"], Decision.SUPPLEMENT)
        self.assertEqual(
            [d["evidence_id"] for d in self.service.pending_reviews()],
            [changed.evidence_id],
        )

        # 拒绝：解释中说明旧结论恢复
        reject_id = self.service.record_decision(
            changed.evidence_id, "R1", Decision.REJECT, "补证后仍不合格"
        )
        explanation = self.service.explain_decision(reject_id)
        self.assertEqual(explanation["decision"], Decision.REJECT)
        self.assertTrue(any("恢复" in rule for rule in explanation["rules"]))
        self.assertEqual(self.service.credit_balance("S1"), {"焊接": 3})


class AsOfReconstructionTest(ServiceTestBase):
    def test_pathway_and_authorization_as_of_date(self):
        self.make_student("S1")
        self.recognize("S1", "焊接", "作品记录-v1")  # DAY1 认定

        self.clock.advance(3)  # DAY1+3：休学
        self.service.suspend("S1")
        self.clock.advance(2)  # DAY1+5：复学并授权
        self.service.resume("S1")
        self.service.grant_disclosure("S1", "ENT-1", {SCOPE_COMPETENCY})
        self.clock.advance(1)  # DAY1+6：转段高职
        self.service.batch_transition(["S1"], Stage.HIGHER)

        day1_view = self.service.pathway_as_of("S1", DAY1)
        self.assertEqual(day1_view["status"], StudentStatus.ACTIVE)
        self.assertEqual(day1_view["stage"], Stage.SECONDARY)
        self.assertEqual(day1_view["credits"], {"焊接": 3})
        self.assertEqual(
            day1_view["plans"][Stage.SECONDARY]["焊接"]["status"], ItemStatus.COMPLETED
        )
        self.assertEqual(day1_view["transitions"], [])

        suspended_view = self.service.pathway_as_of(
            "S1", date(2026, 9, 4)
        )
        self.assertEqual(suspended_view["status"], StudentStatus.SUSPENDED)

        final_view = self.service.pathway_as_of("S1", date(2026, 9, 7))
        self.assertEqual(final_view["stage"], Stage.HIGHER)
        self.assertEqual(len(final_view["transitions"]), 1)

        # 授权状态同样可按日期还原
        self.assertEqual(self.service.authorization_as_of("S1", DAY1), {})
        self.assertEqual(
            self.service.authorization_as_of("S1", date(2026, 9, 6)),
            {"ENT-1": [SCOPE_COMPETENCY]},
        )
        self.assertEqual(
            self.service.enterprise_view("S1", "ENT-1", on_date=DAY1)["granted_scopes"],
            [],
        )


class CombinedStressTest(ServiceTestBase):
    def test_batch_withdrawal_and_outage_together_keep_conservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            service = CredentialService.open(path, clock=self.clock)
            service.publish_standard("STD-WELD", {"焊接": 3})
            service.register_reviewer("R1", {"焊接"})
            for sid in ("S1", "S2", "S3"):
                service.create_plan(
                    sid, "智能制造", Stage.SECONDARY, [("焊接", "STD-WELD")]
                )
                submission = service.submit_evidence(sid, "焊接", f"作品-{sid}")
                service.record_decision(
                    submission.evidence_id, "R1", Decision.RECOGNIZE, "证据充分"
                )
            service.open_posting("P1", "ENT-1", "焊接", 1)
            service.open_posting("P2", "ENT-2", "焊接", 1)
            service.apply_for_slot("S1", "P1")
            service.apply_for_slot("S2", "P2")
            service.apply_for_slot("S3", "P1")  # 候补

            # 批量转段、岗位撤回同时发生
            service.withdraw_posting("P1")
            service.batch_transition(["S1", "S2", "S3"], Stage.HIGHER)

            # 服务中断后恢复：学分与容量仍然守恒
            recovered = CredentialService.open(path, clock=self.clock)
            self.assertEqual(recovered.verify_invariants(), [])
            for sid in ("S1", "S2", "S3"):
                self.assertEqual(recovered.credit_balance(sid), {"焊接": 3})
                self.assertEqual(recovered.student_info(sid)["stage"], Stage.HIGHER)
            # S1 被撤回后以优先候补身份进入 P2 候补，原 P1 候补 S3 紧随其后
            self.assertEqual(recovered.posting_state("P2")["holders"], ["S2"])
            self.assertEqual(recovered.posting_state("P2")["waitlist"], ["S1", "S3"])


if __name__ == "__main__":
    unittest.main()
