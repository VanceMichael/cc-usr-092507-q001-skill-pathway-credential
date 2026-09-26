"""产教贯通能力凭证承接后端。"""

from .context import load_context
from .domain import (
    ALL_SCOPES,
    SCOPE_COMPETENCY,
    SCOPE_CREDENTIALS,
    SCOPE_PATHWAY,
    BatchTransitionError,
    Decision,
    DomainError,
    FixedClock,
    Stage,
    StudentStatus,
)
from .events import Event, EventStore
from .service import CredentialService, Submission

__all__ = [
    "load_context",
    "CredentialService",
    "Submission",
    "Event",
    "EventStore",
    "Stage",
    "StudentStatus",
    "Decision",
    "DomainError",
    "BatchTransitionError",
    "FixedClock",
    "ALL_SCOPES",
    "SCOPE_COMPETENCY",
    "SCOPE_CREDENTIALS",
    "SCOPE_PATHWAY",
]
