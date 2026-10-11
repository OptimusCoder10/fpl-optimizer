"""Offline manager endpoint boundaries and explicit import/evidence inputs.

No names are consumed or retained. Completeness is supplied by the transport
layer: a syntactically valid JSON list alone cannot prove a complete response.
"""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, RootModel,
    model_validator,
)

from fpl_optimizer.schemas.bootstrap import (
    BootstrapBoundaryModel, NullableDatetimeString, SeasonId,
    require_aware_datetime,
)

PositiveInt = Annotated[int, Field(gt=0)]
Money = Annotated[int, Field(ge=0)]


def _required_timestamp(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("timestamp is required")
    return require_aware_datetime(value)


TimestampString = Annotated[NullableDatetimeString, AfterValidator(_required_timestamp)]
ResponseStatus = Literal["complete", "failed", "truncated", "unavailable", "invalid"]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class RawResponse(EvidenceModel):
    """Caller attests transport completeness; failed payloads are never parsed."""

    status: ResponseStatus
    fetched_at: AwareDatetime | None
    payload: object = None

    @model_validator(mode="after")
    def complete_has_time(self):
        if self.status == "complete" and self.fetched_at is None:
            raise ValueError("complete responses require a fetch time")
        return self


T = TypeVar("T")


class ParsedResponse(EvidenceModel, Generic[T]):
    status: ResponseStatus
    fetched_at: AwareDatetime | None
    data: T | None


class Entry(BootstrapBoundaryModel):
    id: PositiveInt
    started_event: PositiveInt
    current_event: PositiveInt | None
    last_deadline_bank: Money | None
    last_deadline_value: Money | None


class EventHistory(BootstrapBoundaryModel):
    event: PositiveInt
    bank: Money
    value: Money
    event_transfers: Money
    event_transfers_cost: Money


class Chip(BootstrapBoundaryModel):
    name: str = Field(min_length=1)
    event: PositiveInt
    time: TimestampString


class History(BootstrapBoundaryModel):
    current: list[EventHistory]
    chips: list[Chip]

    @model_validator(mode="after")
    def unique_events(self):
        for rows in (self.current, self.chips):
            events = [row.event for row in rows]
            if len(events) != len(set(events)):
                raise ValueError("duplicate history/chip event")
        return self


class Transfer(BootstrapBoundaryModel):
    element_in: PositiveInt
    element_in_cost: PositiveInt
    element_out: PositiveInt
    element_out_cost: Money
    entry: PositiveInt
    event: PositiveInt
    time: TimestampString

    @model_validator(mode="after")
    def different_players(self):
        if self.element_in == self.element_out:
            raise ValueError("transfer must change the player")
        return self


class Transfers(RootModel[list[Transfer]]):
    @model_validator(mode="after")
    def unique_records(self):
        keys = [(r.event, r.time, r.element_in, r.element_out) for r in self.root]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate transfer record")
        return self


class Pick(BootstrapBoundaryModel):
    element: PositiveInt
    position: int = Field(ge=1, le=15)
    multiplier: int = Field(ge=0, le=3)
    is_captain: bool
    is_vice_captain: bool


class AutomaticSubstitution(BootstrapBoundaryModel):
    entry: PositiveInt
    element_in: PositiveInt
    element_out: PositiveInt
    event: PositiveInt


class Picks(BootstrapBoundaryModel):
    active_chip: str | None
    entry_history: EventHistory
    picks: list[Pick] = Field(min_length=15, max_length=15)
    automatic_subs: list[AutomaticSubstitution]

    @model_validator(mode="after")
    def valid_squad(self):
        if len({p.element for p in self.picks}) != 15:
            raise ValueError("picks must contain 15 distinct players")
        if {p.position for p in self.picks} != set(range(1, 16)):
            raise ValueError("picks must contain positions 1 through 15")
        captains = [p for p in self.picks if p.is_captain]
        vice = [p for p in self.picks if p.is_vice_captain]
        if len(captains) != 1 or len(vice) != 1 or captains[0] == vice[0]:
            raise ValueError("picks require distinct captain and vice captain")
        return self


class ImportInputs(EvidenceModel):
    season_id: SeasonId
    entry_id: PositiveInt
    entry: RawResponse
    history: RawResponse
    transfers: RawResponse
    # Include explicit event 1 when available, plus any earlier snapshot fallback.
    picks_by_event: dict[PositiveInt, RawResponse]
    deadlines: dict[PositiveInt, AwareDatetime]
    imported_at: AwareDatetime


class ConfirmedOwnership(EvidenceModel):
    """Explicit current ownership, never inferred from public picks."""

    season_id: SeasonId
    entry_id: PositiveInt
    player_ids: tuple[PositiveInt, ...] = Field(min_length=15, max_length=15)
    confirmed_at: AwareDatetime
    ownership_breaks: frozenset[PositiveInt] = frozenset()
    ambiguous_player_ids: frozenset[PositiveInt] = frozenset()

    @model_validator(mode="after")
    def distinct_players(self):
        if len(set(self.player_ids)) != 15:
            raise ValueError("confirmed ownership requires 15 distinct players")
        return self


class MarketPrice(EvidenceModel):
    season_id: SeasonId
    player_id: PositiveInt
    value: Money
    source_observed_at: AwareDatetime
    publication_version: PositiveInt


class GW1ValueRow(EvidenceModel):
    """Projection of already ingested player-fixture history, not an API call."""

    fixture_id: PositiveInt
    round: PositiveInt
    value: Money | None


class PlayerValueHistory(EvidenceModel):
    season_id: SeasonId
    player_id: PositiveInt
    source_observed_at: AwareDatetime
    publication_version: PositiveInt
    rows: tuple[GW1ValueRow, ...]


class ImportIssue(EvidenceModel):
    severity: Literal["warn", "info"]
    code: str


class PublishedSquad(EvidenceModel):
    event: PositiveInt
    deadline: AwareDatetime | None
    fetched_at: AwareDatetime
    picks: Picks
    label: Literal["published snapshot"] = "published snapshot"


class ManagerSnapshot(EvidenceModel):
    season_id: SeasonId
    entry_id: PositiveInt
    imported_at: AwareDatetime
    entry: ParsedResponse[Entry]
    history: ParsedResponse[History]
    transfers: ParsedResponse[Transfers]
    picks_by_event: dict[PositiveInt, ParsedResponse[Picks]]
    latest_public: PublishedSquad | None
    permanent_snapshot: PublishedSquad | None
    issues: tuple[ImportIssue, ...]
    requires_confirmation: Literal[True] = True
    # Public history cannot establish any of these current values.
    remaining_free_transfers: None = None
    current_bank: None = None
    transfers_already_made: None = None
    current_chip: None = None


class GW1Checks(EvidenceModel):
    current_and_gw1_ownership: bool
    history_begins_at_one: bool
    complete_transfers_without_permanent_records: bool
    no_ownership_contradiction: bool
    consistent_positive_gw1_value: bool


class ClassifiedTransfer(EvidenceModel):
    # FPL transfers have no record ID: retain the full event/time/player key.
    record: Transfer
    classification: Literal["permanent", "free_hit", "ambiguous"]


class SalePriceEvidence(EvidenceModel):
    season_id: SeasonId
    entry_id: PositiveInt
    player_id: PositiveInt
    source: Literal["published purchase", "assumed held since GW1", "unresolved"]
    purchase_price: Money | None
    suggested_sale_price: Money | None
    calculated_at: AwareDatetime
    ownership_confirmed_at: AwareDatetime | None
    market: MarketPrice | None
    gw1_history: PlayerValueHistory | None
    checks: GW1Checks
    reasons: tuple[str, ...]
    transfers: tuple[ClassifiedTransfer, ...]
    purchase_record: Transfer | None
    source_fetch_times: dict[str, AwareDatetime | None]
    source_statuses: dict[str, ResponseStatus]
    chip_history: tuple[Chip, ...]
    picks_chip_evidence: dict[PositiveInt, str | None]
    gw1_pick_event: int | None
    manager_history_events: tuple[int, ...]
    # Price suggestions do not constitute confirmation or solver input.
    requires_price_confirmation: Literal[True] = True
    retain_if_unresolved: bool


class ConfirmedSalePrice(EvidenceModel):
    evidence: SalePriceEvidence
    sale_price: Money
    confirmed_at: AwareDatetime
    source: Literal["confirmed suggestion", "manager override"]
