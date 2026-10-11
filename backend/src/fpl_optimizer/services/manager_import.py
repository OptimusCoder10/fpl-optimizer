"""Pure manager import and sale-price suggestions (§04B/E, §05A, §06B/F).

All endpoint responses, transport outcomes, times, market prices and ingested
history are explicit inputs. This module neither fetches nor stores anything.
"""

from datetime import datetime
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from fpl_optimizer.schemas.bootstrap import require_aware_datetime
from fpl_optimizer.schemas.manager_import import (
    ClassifiedTransfer, ConfirmedOwnership, ConfirmedSalePrice, Entry, GW1Checks,
    History, ImportInputs, ImportIssue, ManagerSnapshot, MarketPrice,
    ParsedResponse, Picks, PlayerValueHistory, PublishedSquad, RawResponse,
    SalePriceEvidence, Transfers,
)

PERMANENT_CHIPS = {None, "wildcard", "bboost", "3xc"}
T = TypeVar("T", bound=BaseModel)


def _parse(response: RawResponse, model: type[T]) -> ParsedResponse[T]:
    if response.status != "complete":
        return ParsedResponse[model](
            status=response.status, fetched_at=response.fetched_at, data=None,
        )
    try:
        data = model.model_validate(response.payload)
    except ValidationError:
        # Invalid optional evidence disables that evidence, not the whole import.
        # Do not retain validation errors containing arbitrary personal payloads.
        return ParsedResponse[model](
            status="invalid", fetched_at=response.fetched_at, data=None,
        )
    return ParsedResponse[model](
        status="complete", fetched_at=response.fetched_at, data=data,
    )


def _chip_kind(
    event: int, history: ParsedResponse[History],
    picks_by_event: dict[int, ParsedResponse[Picks]],
) -> Literal["permanent", "free_hit", "ambiguous"]:
    """Positive Free Hit evidence is an explicit chip record for this event.

    Absence in a *complete* chip history establishes ordinary published activity.
    Unknown chip names, missing coverage or contradictory sources are ambiguous.
    """
    chip_names = []
    if history.data is not None and event in {r.event for r in history.data.current}:
        chip_names.append(next(
            (c.name for c in history.data.chips if c.event == event), None,
        ))
    response = picks_by_event.get(event)
    if response is not None and response.data is not None:
        chip_names.append(response.data.active_chip)
    if not chip_names or len(set(chip_names)) != 1:
        return "ambiguous"
    if chip_names[0] == "freehit":
        return "free_hit"
    if chip_names[0] in PERMANENT_CHIPS:
        return "permanent"
    return "ambiguous"


def parse_manager_import(inputs: ImportInputs) -> ManagerSnapshot:
    """Validate independent responses and retain historical provenance.

    A wrong entry/event association is a caller error, not missing evidence.
    Missing/invalid optional responses remain explicit states in the snapshot.
    The caller can supply earlier picks when the newest are unavailable or FH.
    """
    entry = _parse(inputs.entry, Entry)
    history = _parse(inputs.history, History)
    transfers = _parse(inputs.transfers, Transfers)
    picks = {event: _parse(raw, Picks) for event, raw in inputs.picks_by_event.items()}
    if entry.data is not None and entry.data.id != inputs.entry_id:
        raise ValueError("entry response belongs to a different manager")
    if transfers.data is not None:
        if any(t.entry != inputs.entry_id for t in transfers.data.root):
            raise ValueError("transfer response belongs to a different manager")
        if any(t.time > transfers.fetched_at for t in transfers.data.root):
            raise ValueError("transfer time cannot follow its fetch time")
    if history.data is not None:
        if any(c.time > history.fetched_at for c in history.data.chips):
            raise ValueError("chip time cannot follow its fetch time")
    for event, response in picks.items():
        if response.data is None:
            continue
        if response.data.entry_history.event != event:
            raise ValueError("picks response belongs to a different event")
        if any(s.entry != inputs.entry_id or s.event != event
               for s in response.data.automatic_subs):
            raise ValueError("automatic substitution belongs to a different entry/event")

    responses = {"entry": entry, "history": history, "transfers": transfers}
    responses.update({f"picks:{event}": response for event, response in picks.items()})
    for response in responses.values():
        if response.fetched_at is not None and response.fetched_at > inputs.imported_at:
            raise ValueError("fetch time cannot follow import time")
    issues = [ImportIssue(severity="warn", code=f"{name}:{r.status}")
              for name, r in responses.items() if r.data is None]
    snapshots = [PublishedSquad(
        event=event, deadline=inputs.deadlines.get(event),
        fetched_at=response.fetched_at, picks=response.data,
    ) for event, response in sorted(picks.items(), reverse=True)
        if response.data is not None]
    latest = snapshots[0] if snapshots else None
    permanent = next((s for s in snapshots
                      if _chip_kind(s.event, history, picks) == "permanent"), None)
    if latest is None:
        issues.append(ImportIssue(severity="warn", code="manual_entry_required"))
    elif _chip_kind(latest.event, history, picks) != "permanent":
        issues.append(ImportIssue(severity="warn", code="temporary_or_ambiguous_snapshot"))
        if permanent is None:
            issues.append(ImportIssue(severity="warn", code="manual_entry_required"))
    if history.data and history.data.current:
        if min(r.event for r in history.data.current) == 1 and 1 not in picks:
            issues.append(ImportIssue(severity="warn", code="gw1_picks_unavailable"))
    issues.append(ImportIssue(severity="info", code="confirm_current_state_and_prices"))
    return ManagerSnapshot(
        season_id=inputs.season_id, entry_id=inputs.entry_id,
        imported_at=inputs.imported_at, entry=entry, history=history,
        transfers=transfers, picks_by_event=picks, latest_public=latest,
        permanent_snapshot=permanent, issues=tuple(issues),
    )


def calculate_sale_price(purchase_price: int, current_price: int) -> int:
    """Integer tenths only; retain half of a rise, rounded down."""
    if type(purchase_price) is not int or purchase_price <= 0:
        raise ValueError("purchase price must be positive integer tenths")
    if type(current_price) is not int or current_price < 0:
        raise ValueError("current price must be non-negative integer tenths")
    return (current_price if current_price <= purchase_price
            else purchase_price + (current_price - purchase_price) // 2)


def suggest_sale_price(
    snapshot: ManagerSnapshot, *, player_id: int,
    ownership: ConfirmedOwnership | None,
    market: MarketPrice | None,
    gw1_history: PlayerValueHistory | None,
    calculated_at: datetime,
) -> SalePriceEvidence:
    """Resolve one player's price; unrelated missing evidence never blocks others.

    Permanent transactions are replayed in time order. A sell resets ownership;
    a later buy establishes a new cost. Inconsistent or same-time per-player
    transactions require correction rather than an invented ordering.
    """
    require_aware_datetime(calculated_at)
    if type(player_id) is not int or player_id <= 0:
        raise ValueError("player ID must be a positive integer")
    if calculated_at < snapshot.imported_at:
        raise ValueError("calculation cannot precede import")
    if ownership is not None:
        if (ownership.season_id, ownership.entry_id) != (snapshot.season_id, snapshot.entry_id):
            raise ValueError("ownership belongs to a different season/entry")
        if ownership.confirmed_at > calculated_at:
            raise ValueError("ownership confirmation cannot follow calculation")
    for evidence in (market, gw1_history):
        if evidence is not None:
            if (evidence.season_id, evidence.player_id) != (snapshot.season_id, player_id):
                raise ValueError("price evidence belongs to a different season/player")
            if evidence.source_observed_at > calculated_at:
                raise ValueError("price evidence cannot follow calculation")

    current = ownership is not None and player_id in ownership.player_ids
    history_events = tuple(sorted(r.event for r in snapshot.history.data.current)) if snapshot.history.data else ()
    starts_at_one = bool(history_events) and history_events[0] == 1
    gw1_response = snapshot.picks_by_event.get(1)
    gw1_picks = gw1_response.data if gw1_response else None
    in_gw1 = gw1_picks is not None and player_id in {p.element for p in gw1_picks.picks}
    classified = tuple(ClassifiedTransfer(
        record=t, classification=_chip_kind(t.event, snapshot.history, snapshot.picks_by_event),
    ) for t in sorted(snapshot.transfers.data.root, key=lambda r: (
        r.time, r.event, r.element_in, r.element_out,
    )) if player_id in (t.element_in, t.element_out)) if snapshot.transfers.data else ()
    permanent = [t.record for t in classified if t.classification == "permanent"]
    contradictions = []
    if ownership and player_id in ownership.ownership_breaks:
        contradictions.append("manager_reported_ownership_break")
    if ownership and player_id in ownership.ambiguous_player_ids:
        contradictions.append("manager_reported_ambiguity")
    if any(t.classification == "ambiguous" for t in classified):
        contradictions.append("ambiguous_chip_activity")
    if gw1_picks and _chip_kind(1, snapshot.history, snapshot.picks_by_event) != "permanent":
        contradictions.append("gw1_chip_ambiguous_or_temporary")
    if snapshot.entry.data and history_events:
        if snapshot.entry.data.started_event != history_events[0]:
            contradictions.append("entry_history_start_conflict")

    held = in_gw1 if starts_at_one and gw1_picks is not None else None
    purchase = None
    prior_time = None
    prior_event = None
    for record in permanent:
        if record.time == prior_time or (prior_event is not None and record.event < prior_event):
            contradictions.append("ambiguous_transfer_order")
        prior_time, prior_event = record.time, record.event
        if record.element_in == player_id:
            if held is True:
                contradictions.append("inbound_without_ownership_break")
            held, purchase = True, record
        else:
            if held is False:
                contradictions.append("outbound_without_ownership")
            held, purchase = False, None
    if permanent and held is False and current:
        contradictions.append("published_sale_conflicts_with_current_ownership")

    # Available permanent picks are additional ownership evidence. Do not let a
    # nominally complete but inconsistent transfer list erase an observed break.
    for event, response in snapshot.picks_by_event.items():
        if response.data is None:
            continue
        kind = _chip_kind(event, snapshot.history, snapshot.picks_by_event)
        if kind == "ambiguous":
            contradictions.append("ambiguous_snapshot_chip")
            continue
        if kind == "free_hit":
            continue
        expected = in_gw1 if starts_at_one and gw1_picks is not None else None
        for record in permanent:
            if record.event <= event:
                expected = record.element_in == player_id
        observed = player_id in {p.element for p in response.data.picks}
        if expected is not None and observed != expected:
            contradictions.append("published_picks_conflict_with_ownership_history")

    rows = tuple(r for r in gw1_history.rows if r.round == 1) if gw1_history else ()
    values = {r.value for r in rows}
    valid_value = (bool(rows) and len(values) == 1 and None not in values
                   and next(iter(values)) > 0
                   and len({r.fixture_id for r in rows}) == len(rows))
    checks = GW1Checks(
        current_and_gw1_ownership=current and in_gw1,
        history_begins_at_one=starts_at_one,
        complete_transfers_without_permanent_records=(
            snapshot.transfers.status == "complete" and not permanent
        ),
        no_ownership_contradiction=not contradictions,
        consistent_positive_gw1_value=valid_value,
    )
    source = "unresolved"
    cost = None
    reasons = list(dict.fromkeys(contradictions))
    if not current:
        reasons.append("current_ownership_not_confirmed")
    if market is None:
        reasons.append("market_price_unavailable")
    if current and market is not None and not contradictions:
        if snapshot.transfers.status == "complete" and purchase is not None:
            source, cost = "published purchase", purchase.element_in_cost
        elif all(checks.model_dump().values()):
            source, cost = "assumed held since GW1", rows[0].value
    if source == "unresolved":
        reasons.extend(name for name, passed in checks.model_dump().items() if not passed)
    fetch_times = {
        "entry": snapshot.entry.fetched_at,
        "history": snapshot.history.fetched_at,
        "transfers": snapshot.transfers.fetched_at,
    }
    fetch_times.update({f"picks:{event}": r.fetched_at
                        for event, r in snapshot.picks_by_event.items()})
    source_statuses = {
        "entry": snapshot.entry.status, "history": snapshot.history.status,
        "transfers": snapshot.transfers.status,
    }
    source_statuses.update({f"picks:{event}": r.status
                           for event, r in snapshot.picks_by_event.items()})
    return SalePriceEvidence(
        season_id=snapshot.season_id, entry_id=snapshot.entry_id, player_id=player_id,
        source=source, purchase_price=cost,
        suggested_sale_price=calculate_sale_price(cost, market.value) if cost is not None else None,
        calculated_at=calculated_at,
        ownership_confirmed_at=ownership.confirmed_at if current else None,
        market=market, gw1_history=gw1_history, checks=checks,
        reasons=tuple(dict.fromkeys(reasons)), transfers=classified,
        purchase_record=purchase if source == "published purchase" else None,
        source_fetch_times=fetch_times, gw1_pick_event=1 if in_gw1 else None,
        source_statuses=source_statuses,
        chip_history=tuple(snapshot.history.data.chips) if snapshot.history.data else (),
        picks_chip_evidence={event: r.data.active_chip
                             for event, r in snapshot.picks_by_event.items() if r.data},
        manager_history_events=history_events, retain_if_unresolved=source == "unresolved",
    )


def confirm_sale_price(
    evidence: SalePriceEvidence, *, sale_price: int, confirmed_at: datetime,
) -> ConfirmedSalePrice:
    """Record an editable suggestion/override without erasing its source evidence."""
    require_aware_datetime(confirmed_at)
    if evidence.market is None:
        raise ValueError("current market price is required to validate a sale price")
    if type(sale_price) is not int or not 0 <= sale_price <= evidence.market.value:
        raise ValueError("selling price must be integer tenths between zero and market price")
    if evidence.ownership_confirmed_at is None:
        raise ValueError("confirm current ownership before confirming a selling price")
    if confirmed_at < evidence.calculated_at:
        raise ValueError("price confirmation cannot precede calculation")
    return ConfirmedSalePrice(
        evidence=evidence, sale_price=sale_price, confirmed_at=confirmed_at,
        source=("confirmed suggestion" if sale_price == evidence.suggested_sale_price
                else "manager override"),
    )
