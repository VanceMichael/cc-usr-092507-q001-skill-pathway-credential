"""领域基础：学段、状态、决策类型、披露范围与业务异常。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


class Stage:
    """贯通培养的三个学段。"""

    SECONDARY = "secondary_vocational"  # 中职
    HIGHER = "higher_vocational"  # 高职
    BACHELOR = "applied_bachelor"  # 应用型本科


STAGE_ORDER = [Stage.SECONDARY, Stage.HIGHER, Stage.BACHELOR]


class StudentStatus:
    ACTIVE = "active"  # 在籍
    SUSPENDED = "suspended"  # 休学


class Decision:
    RECOGNIZE = "recognize"  # 承认
    REJECT = "reject"  # 拒绝
    SUPPLEMENT = "supplement"  # 补证


DECISIONS = {Decision.RECOGNIZE, Decision.REJECT, Decision.SUPPLEMENT}


class EvidenceStatus:
    PENDING = "pending"  # 待复核
    RECOGNIZED = "recognized"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # 被新提交取代


class ConclusionStatus:
    ACTIVE = "active"
    SUSPENDED = "suspended"  # 暂停折算，等待核查
    REPLACED = "replaced"


class ItemStatus:
    PENDING = "pending"
    COMPLETED = "completed"


# 披露范围：学生可授权企业查看的能力摘要类别
SCOPE_COMPETENCY = "competency_summary"  # 能力摘要
SCOPE_CREDENTIALS = "credentials"  # 凭证清单
SCOPE_PATHWAY = "pathway"  # 培养路径
ALL_SCOPES = frozenset({SCOPE_COMPETENCY, SCOPE_CREDENTIALS, SCOPE_PATHWAY})


class DomainError(Exception):
    """业务规则被拒绝。"""


class BatchTransitionError(DomainError):
    """批量转段整体失败：任何学生不合格都不会产生部分事件。"""

    def __init__(self, errors: dict[str, list[str]]):
        self.errors = errors
        super().__init__(f"批量转段失败：{errors}")


class SystemClock:
    def today(self) -> date:
        return date.today()


@dataclass
class FixedClock:
    """测试用时钟，可显式推进。"""

    current: date

    def today(self) -> date:
        return self.current

    def advance(self, days: int) -> None:
        self.current += timedelta(days=days)
