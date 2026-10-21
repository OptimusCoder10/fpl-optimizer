"""Transport outcomes shared by the HTTP boundary and application adapters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from fpl_optimizer.schemas.manager_import import RawResponse

T = TypeVar("T", bound=BaseModel)
FetchStatus = Literal["success", "not_found", "transient_failure", "truncated_or_invalid"]
FailureCategory = Literal[
    "not_found", "transient_failure", "truncated_or_invalid", "incomplete_fixtures",
    "budget_exhausted",
]


class FetchFailure(Exception):
    """Safe category only; never carry URLs, response bodies or exception text."""

    def __init__(self, category: FailureCategory, *, stop_run: bool = False):
        super().__init__(category)
        self.category = category
        self.stop_run = stop_run


@dataclass(frozen=True)
class FetchResult(Generic[T]):
    status: FetchStatus
    fetched_at: datetime
    data: T | None = None
    category: FailureCategory | None = None
    budget_exhausted: bool = False
    raw_shared_payload: Any | None = None

    def __post_init__(self):
        if (self.status == "success") != (self.data is not None):
            raise ValueError("only successful fetches contain data")
        if (self.status == "success") != (self.category is None):
            raise ValueError("failed fetches require a category")
        if self.budget_exhausted and self.status == "success":
            raise ValueError("exhausted fetch cannot succeed")

    def require_data(self) -> T:
        if self.data is None:
            raise FetchFailure(self.category, stop_run=self.budget_exhausted)
        return self.data

    def manager_response(self) -> RawResponse:
        """Completeness derives only from the fetch outcome, never a default []."""
        status = {
            "success": "complete", "not_found": "unavailable",
            "transient_failure": "failed", "truncated_or_invalid": "invalid",
        }[self.status]
        return RawResponse(status=status, fetched_at=self.fetched_at, payload=self.data)
