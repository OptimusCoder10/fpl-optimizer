"""Slice 1.6 proofs using invented fixtures only; no real manager data or HTTP."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fpl_optimizer.schemas.manager_import import (
    ConfirmedOwnership, GW1ValueRow, ImportInputs, MarketPrice, PlayerValueHistory,
    RawResponse,
)
from fpl_optimizer.services.manager_import import (
    calculate_sale_price, confirm_sale_price, parse_manager_import, suggest_sale_price,
)


NOW = datetime(2030, 10, 1, tzinfo=timezone.utc)
FETCHED = NOW - timedelta(minutes=5)
SEASON = "2030-31"
SYNTHETIC_ENTRY_ID = 2147483647


@pytest.fixture
def payloads():
    return json.loads((Path(__file__).parent / "fixtures/manager_import/synthetic.json").read_text())


def response(payload=None, status="complete"):
    return RawResponse(status=status, fetched_at=FETCHED, payload=payload)


def inputs(raw):
    assert raw["synthetic"] is True
    return ImportInputs(
        season_id=SEASON, entry_id=SYNTHETIC_ENTRY_ID,
        entry=response(raw["entry"]), history=response(raw["history"]),
        transfers=response(raw["transfers"]),
        picks_by_event={1: response(raw["gw1_picks"]), 5: response(raw["latest_picks"])},
        deadlines={1: NOW - timedelta(days=35), 5: NOW - timedelta(days=7)},
        imported_at=NOW,
    )


def ownership(**changes):
    return ConfirmedOwnership(**(dict(
        season_id=SEASON, entry_id=SYNTHETIC_ENTRY_ID,
        player_ids=tuple(range(1, 16)), confirmed_at=NOW,
    ) | changes))


def market(value=55, player_id=1):
    return MarketPrice(season_id=SEASON, player_id=player_id, value=value,
                       source_observed_at=FETCHED, publication_version=3)


def values(*costs):
    return PlayerValueHistory(
        season_id=SEASON, player_id=1, source_observed_at=FETCHED,
        publication_version=7,
        rows=tuple(GW1ValueRow(fixture_id=i, round=1, value=c)
                   for i, c in enumerate(costs, 1)),
    )


def price(snapshot, **changes):
    return suggest_sale_price(snapshot, **(dict(
        player_id=1, ownership=ownership(), market=market(),
        gw1_history=values(50), calculated_at=NOW,
    ) | changes))


def transaction(event, player_in, player_out, cost=50, day=None):
    return dict(entry=SYNTHETIC_ENTRY_ID, event=event, element_in=player_in,
                element_out=player_out, element_in_cost=cost, element_out_cost=50,
                time=f"2030-09-{day or event:02d}T12:00:00Z")


def chip(raw, event, name="freehit"):
    raw["history"]["chips"].append(dict(name=name, event=event, time="2030-09-01T12:00:00Z"))


def test_snapshot_is_historical_typed_and_does_not_invent_current_state(payloads):
    snapshot = parse_manager_import(inputs(payloads))
    assert snapshot.latest_public.event == 5
    assert snapshot.latest_public.deadline == NOW - timedelta(days=7)
    assert snapshot.latest_public.fetched_at == FETCHED
    assert snapshot.latest_public.label == "published snapshot"
    assert snapshot.permanent_snapshot == snapshot.latest_public
    assert snapshot.entry.data.last_deadline_bank == 10
    assert snapshot.remaining_free_transfers is None
    assert snapshot.transfers_already_made is None
    assert snapshot.current_chip is None and snapshot.current_bank is None
    assert snapshot.requires_confirmation
    assert snapshot.latest_public.picks.picks[0].is_captain


def test_all_five_gw1_checks_pass_with_auditable_assumption(payloads):
    result = price(parse_manager_import(inputs(payloads)), gw1_history=values(50, 50))
    assert all(result.checks.model_dump().values())
    assert result.source == "assumed held since GW1"
    assert (result.purchase_price, result.suggested_sale_price) == (50, 52)
    assert result.gw1_pick_event == 1 and result.manager_history_events == (1, 2, 3, 4, 5)
    assert result.source_fetch_times["picks:1"] == FETCHED
    assert result.gw1_history.rows[1].fixture_id == 2
    assert result.gw1_history.publication_version == 7
    assert result.market.publication_version == 3
    assert result.calculated_at == NOW and result.ownership_confirmed_at == NOW
    assert result.requires_price_confirmation and not result.retain_if_unresolved
    assert result.purchase_record is None and result.reasons == ()
    assert "verified" not in result.model_dump_json()


@pytest.mark.parametrize("failed_check", [
    "current_and_gw1_ownership", "history_begins_at_one",
    "complete_transfers_without_permanent_records", "no_ownership_contradiction",
    "consistent_positive_gw1_value",
])
def test_each_gw1_condition_fails_independently(payloads, failed_check):
    supplied = {}
    if failed_check == "current_and_gw1_ownership":
        supplied["ownership"] = ownership(player_ids=tuple(range(2, 17)))
    if failed_check == "history_begins_at_one":
        payloads["history"]["current"].pop(0)
        payloads["entry"]["started_event"] = 2
    if failed_check == "no_ownership_contradiction":
        supplied["ownership"] = ownership(ownership_breaks=frozenset({1}))
    if failed_check == "consistent_positive_gw1_value":
        supplied["gw1_history"] = values(50, 51)
    request = inputs(payloads)
    if failed_check == "complete_transfers_without_permanent_records":
        request = request.model_copy(update={"transfers": response([], "failed")})
    result = price(parse_manager_import(request), **supplied)
    assert result.checks.model_dump() == {
        key: key != failed_check for key in result.checks.model_dump()
    }
    assert result.source == "unresolved"
    assert result.suggested_sale_price is None and result.purchase_price is None
    assert failed_check in result.reasons and result.retain_if_unresolved


@pytest.mark.parametrize("status", ["failed", "truncated", "unavailable", "invalid"])
def test_unsuccessful_transfers_cannot_be_treated_as_empty(payloads, status):
    request = inputs(payloads).model_copy(update={"transfers": response([], status)})
    snapshot = parse_manager_import(request)
    assert snapshot.transfers.status == status and snapshot.transfers.data is None
    assert snapshot.latest_public.event == 5
    assert price(snapshot).source == "unresolved"


@pytest.mark.parametrize("bad_payload", [{}, [dict(event=3)], "[]"])
def test_malformed_transfers_are_not_empty(payloads, bad_payload):
    payloads["transfers"] = bad_payload
    snapshot = parse_manager_import(inputs(payloads))
    assert snapshot.transfers.status == "invalid"
    assert price(snapshot).source == "unresolved"


def test_rebuy_starts_a_new_spell_and_response_order_does_not_matter(payloads):
    payloads["transfers"] = [transaction(4, 1, 20, 54), transaction(2, 20, 1)]
    result = price(parse_manager_import(inputs(payloads)), market=market(59))
    assert result.source == "published purchase"
    assert (result.purchase_price, result.suggested_sale_price) == (54, 56)
    assert result.purchase_record.event == 4
    assert [t.record.event for t in result.transfers] == [2, 4]
    assert not result.checks.complete_transfers_without_permanent_records


def test_original_non_gw1_purchase_does_not_need_gw1_value(payloads):
    payloads["gw1_picks"]["picks"][0]["element"] = 20
    payloads["transfers"] = [transaction(3, 1, 20, 52)]
    result = price(parse_manager_import(inputs(payloads)), gw1_history=None)
    assert result.source == "published purchase" and result.purchase_price == 52


@pytest.mark.parametrize(("purchase", "current", "expected"), [
    (50, 50, 50), (50, 51, 50), (50, 52, 51), (50, 55, 52),
    (50, 56, 53), (50, 49, 49), (50, 40, 40), (50, 0, 0),
])
def test_price_rises_odd_even_tenths_and_falls(purchase, current, expected):
    assert calculate_sale_price(purchase, current) == expected


@pytest.mark.parametrize(("purchase", "current"), [(0, 50), (-1, 50), (True, 50), (50, -1), (50, 5.5)])
def test_sale_formula_rejects_invalid_units(purchase, current):
    with pytest.raises(ValueError):
        calculate_sale_price(purchase, current)


def test_positively_identified_free_hit_sale_is_excluded(payloads):
    chip(payloads, 3)
    payloads["transfers"] = [transaction(3, 20, 1)]
    result = price(parse_manager_import(inputs(payloads)))
    assert result.source == "assumed held since GW1"
    assert result.transfers[0].classification == "free_hit"
    assert result.chip_history[0].event == 3
    assert result.chip_history[0].name == "freehit"
    assert all(result.checks.model_dump().values())


def test_free_hit_inbound_does_not_replace_permanent_purchase(payloads):
    payloads["gw1_picks"]["picks"][0]["element"] = 20
    chip(payloads, 4)
    payloads["transfers"] = [transaction(2, 1, 20, 51), transaction(4, 1, 21, 60)]
    result = price(parse_manager_import(inputs(payloads)))
    assert result.purchase_price == 51 and result.purchase_record.event == 2


@pytest.mark.parametrize("chip_name", [None, "wildcard", "bboost", "3xc", "unknown_chip"])
def test_activity_is_not_assumed_free_hit(payloads, chip_name):
    if chip_name is not None:
        chip(payloads, 3, chip_name)
    payloads["transfers"] = [transaction(3, 20, 1)]
    result = price(parse_manager_import(inputs(payloads)))
    assert result.source == "unresolved"
    assert result.transfers[0].classification != "free_hit"


def test_conflicting_chip_evidence_is_not_excluded(payloads):
    chip(payloads, 5)
    payloads["transfers"] = [transaction(5, 20, 1)]
    result = price(parse_manager_import(inputs(payloads)))
    assert result.transfers[0].classification == "ambiguous"
    assert "ambiguous_chip_activity" in result.reasons


def test_latest_free_hit_snapshot_offers_previous_permanent_and_keeps_provenance(payloads):
    chip(payloads, 5)
    payloads["latest_picks"]["active_chip"] = "freehit"
    request = inputs(payloads)
    previous = deepcopy(payloads["gw1_picks"])
    previous["entry_history"]["event"] = 4
    request.picks_by_event[4] = response(previous)
    snapshot = parse_manager_import(request)
    assert snapshot.latest_public.event == 5
    assert snapshot.permanent_snapshot.event == 4
    assert "temporary_or_ambiguous_snapshot" in {i.code for i in snapshot.issues}
    assert price(snapshot, ownership=None).suggested_sale_price is None


def test_free_hit_without_prior_snapshot_requires_manual_entry(payloads):
    chip(payloads, 5)
    payloads["latest_picks"]["active_chip"] = "freehit"
    request = inputs(payloads)
    request.picks_by_event.pop(1)
    snapshot = parse_manager_import(request)
    assert snapshot.permanent_snapshot is None
    assert "manual_entry_required" in {i.code for i in snapshot.issues}


@pytest.mark.parametrize("endpoint", ["history", "gw1", "market", "value", "ownership"])
def test_missing_evidence_keeps_the_snapshot_but_price_unresolved(payloads, endpoint):
    request = inputs(payloads)
    supplied = {}
    if endpoint == "history":
        request = request.model_copy(update={"history": response(None, "failed")})
    elif endpoint == "gw1":
        request.picks_by_event[1] = response(None, "unavailable")
    else:
        supplied[{"value": "gw1_history"}.get(endpoint, endpoint)] = None
    snapshot = parse_manager_import(request)
    assert snapshot.latest_public.event == 5
    result = price(snapshot, **supplied)
    assert result.source == "unresolved" and result.retain_if_unresolved


@pytest.mark.parametrize("costs", [(), (None,), (0,), (50, None), (50, 0), (50, 51)])
def test_missing_zero_or_conflicting_gw1_values_fail(payloads, costs):
    result = price(parse_manager_import(inputs(payloads)), gw1_history=values(*costs))
    assert not result.checks.consistent_positive_gw1_value
    assert result.source == "unresolved"


def test_unrelated_player_can_resolve_when_one_price_is_missing(payloads):
    snapshot = parse_manager_import(inputs(payloads))
    unresolved = price(snapshot, gw1_history=None)
    history = values(50).model_copy(update={"player_id": 2})
    resolved = price(snapshot, player_id=2, market=market(player_id=2), gw1_history=history)
    assert unresolved.retain_if_unresolved and not resolved.retain_if_unresolved


def test_gw1_is_reused_when_latest_is_one_and_next_picks_unavailable(payloads):
    request = inputs(payloads)
    request.picks_by_event[5] = response(None, "unavailable")
    snapshot = parse_manager_import(request)
    assert snapshot.latest_public.event == 1
    assert snapshot.picks_by_event[1].data == snapshot.latest_public.picks
    assert price(snapshot).source == "assumed held since GW1"


def test_empty_or_unavailable_import_allows_manual_entry(payloads):
    request = inputs(payloads).model_copy(update={
        "entry": response(None, "failed"), "picks_by_event": {},
    })
    snapshot = parse_manager_import(request)
    assert snapshot.latest_public is None
    assert "manual_entry_required" in {i.code for i in snapshot.issues}


def test_confirmation_preserves_assumption_and_override_evidence(payloads):
    evidence = price(parse_manager_import(inputs(payloads)))
    confirmed = confirm_sale_price(evidence, sale_price=52, confirmed_at=NOW)
    assert confirmed.source == "confirmed suggestion"
    assert confirmed.evidence.source == "assumed held since GW1"
    unresolved = price(parse_manager_import(inputs(payloads)), gw1_history=None)
    override = confirm_sale_price(unresolved, sale_price=54, confirmed_at=NOW)
    assert override.source == "manager override"
    assert override.evidence.source == "unresolved" and override.confirmed_at == NOW


@pytest.mark.parametrize("bad_price", [-1, 56, True, 52.5])
def test_invalid_overrides_are_rejected(payloads, bad_price):
    with pytest.raises(ValueError, match="selling price"):
        confirm_sale_price(price(parse_manager_import(inputs(payloads))),
                           sale_price=bad_price, confirmed_at=NOW)


def test_late_joiner_purchase_works_but_no_gw1_cost_is_assumed(payloads):
    payloads["entry"]["started_event"] = 2
    payloads["history"]["current"].pop(0)
    payloads["transfers"] = [transaction(3, 1, 20, 51)]
    request = inputs(payloads)
    request.picks_by_event.pop(1)
    result = price(parse_manager_import(request), gw1_history=None)
    assert result.source == "published purchase" and result.purchase_price == 51
    request = request.model_copy(update={"transfers": response([])})
    assert price(parse_manager_import(request)).source == "unresolved"


@pytest.mark.parametrize("transactions", [
    [transaction(2, 20, 1)],
    [transaction(2, 1, 20)],
    [transaction(2, 20, 1), transaction(3, 21, 1)],
    [transaction(2, 20, 1), transaction(3, 1, 20, day=2)],
    [transaction(3, 20, 1, day=2), transaction(2, 1, 20, day=3)],
])
def test_contradictory_or_ambiguously_ordered_ownership_needs_correction(payloads, transactions):
    payloads["transfers"] = transactions
    result = price(parse_manager_import(inputs(payloads)))
    assert result.source == "unresolved" and not result.checks.no_ownership_contradiction


def test_failed_chip_history_cannot_classify_transfers_as_permanent(payloads):
    payloads["transfers"] = [transaction(3, 1, 20, 51)]
    request = inputs(payloads).model_copy(update={"history": response(None, "failed")})
    result = price(parse_manager_import(request))
    assert result.transfers[0].classification == "ambiguous"
    assert result.source == "unresolved"


def test_free_hit_can_be_positively_identified_by_event_picks(payloads):
    payloads["latest_picks"]["active_chip"] = "freehit"
    payloads["transfers"] = [transaction(5, 20, 1)]
    request = inputs(payloads).model_copy(update={"history": response(None, "failed")})
    result = price(parse_manager_import(request))
    assert result.transfers[0].classification == "free_hit"
    # History is still necessary for GW1 condition 2.
    assert result.source == "unresolved"


@pytest.mark.parametrize("field", ["entry", "history", "picks"])
def test_consumed_endpoint_fields_are_strictly_validated(payloads, field):
    if field == "entry":
        payloads["entry"]["id"] = str(SYNTHETIC_ENTRY_ID)
    elif field == "history":
        payloads["history"]["current"][0]["bank"] = True
    else:
        payloads["latest_picks"]["picks"][0]["is_captain"] = "true"
    snapshot = parse_manager_import(inputs(payloads))
    parsed = snapshot.picks_by_event[5] if field == "picks" else getattr(snapshot, field)
    assert parsed.status == "invalid" and parsed.data is None


@pytest.mark.parametrize("failure", ["count", "player", "position", "captain", "vice"])
def test_invalid_picks_fall_back_to_other_usable_snapshot(payloads, failure):
    picks = payloads["latest_picks"]["picks"]
    if failure == "count":
        picks.pop()
    elif failure == "player":
        picks[1]["element"] = 1
    elif failure == "position":
        picks[1]["position"] = 1
    elif failure == "captain":
        picks[1]["is_captain"] = True
    else:
        picks[1]["is_vice_captain"] = False
    snapshot = parse_manager_import(inputs(payloads))
    assert snapshot.picks_by_event[5].status == "invalid"
    assert snapshot.latest_public.event == 1


@pytest.mark.parametrize("endpoint", ["entry", "transfers", "picks", "automatic_subs"])
def test_cross_endpoint_identity_mismatches_are_rejected(payloads, endpoint):
    if endpoint == "entry":
        payloads["entry"]["id"] -= 1
    elif endpoint == "transfers":
        payloads["transfers"] = [transaction(2, 20, 1) | {"entry": SYNTHETIC_ENTRY_ID - 1}]
    elif endpoint == "picks":
        payloads["latest_picks"]["entry_history"]["event"] = 4
    else:
        payloads["latest_picks"]["automatic_subs"] = [dict(
            entry=SYNTHETIC_ENTRY_ID, element_in=12, element_out=1, event=4,
        )]
    with pytest.raises(ValueError, match="different"):
        parse_manager_import(inputs(payloads))


def test_automatic_substitutions_preserved_without_claiming_original_order(payloads):
    payloads["latest_picks"]["automatic_subs"] = [dict(
        entry=SYNTHETIC_ENTRY_ID, element_in=12, element_out=1, event=5,
    )]
    snapshot = parse_manager_import(inputs(payloads))
    assert snapshot.latest_public.picks.automatic_subs[0].element_in == 12
    assert snapshot.latest_public.picks.picks[0].position == 1


def test_duplicate_transfer_records_are_invalid_not_new_spells(payloads):
    record = transaction(2, 20, 1)
    payloads["transfers"] = [record, record]
    snapshot = parse_manager_import(inputs(payloads))
    assert snapshot.transfers.status == "invalid"
    assert price(snapshot).source == "unresolved"


@pytest.mark.parametrize("field", ["current", "chips"])
def test_duplicate_history_events_disable_history_evidence(payloads, field):
    chip(payloads, 3)
    payloads["history"][field].append(deepcopy(payloads["history"][field][0]))
    snapshot = parse_manager_import(inputs(payloads))
    assert snapshot.history.status == "invalid"
    assert price(snapshot).source == "unresolved"


@pytest.mark.parametrize("input_name", ["market", "gw1_history", "ownership"])
def test_season_identity_mismatch_rejected(payloads, input_name):
    supplied = {"market": market(), "gw1_history": values(50), "ownership": ownership()}
    supplied[input_name] = supplied[input_name].model_copy(update={"season_id": "2029-30"})
    with pytest.raises(ValueError, match="different season"):
        price(parse_manager_import(inputs(payloads)), **supplied)


def test_invalid_confirmation_and_timestamps_rejected(payloads):
    with pytest.raises(ValidationError):
        ownership(player_ids=tuple([1] * 15))
    with pytest.raises(ValidationError):
        ownership(confirmed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        RawResponse(status="complete", payload=[], fetched_at=None)
    with pytest.raises(ValueError, match="precede import"):
        price(parse_manager_import(inputs(payloads)), calculated_at=FETCHED)
    with pytest.raises(ValueError, match="fetch time"):
        parse_manager_import(inputs(payloads).model_copy(update={"imported_at": FETCHED - timedelta(seconds=1)}))


def test_a_sale_price_requires_both_market_and_confirmed_ownership(payloads):
    snapshot = parse_manager_import(inputs(payloads))
    for supplied in ({"market": None}, {"ownership": None}):
        with pytest.raises(ValueError):
            confirm_sale_price(price(snapshot, **supplied), sale_price=50, confirmed_at=NOW)


def test_failed_gw1_response_does_not_disable_established_purchase(payloads):
    payloads["transfers"] = [transaction(3, 1, 20, 51)]
    request = inputs(payloads)
    request.picks_by_event[1] = response(None, "unavailable")
    result = price(parse_manager_import(request))
    assert result.source == "published purchase" and result.purchase_price == 51


def test_non_gw1_rows_cannot_supply_fallback_value(payloads):
    other_rows = values(50).model_copy(update={"rows": (
        GW1ValueRow(fixture_id=1, round=2, value=50),
    )})
    assert price(parse_manager_import(inputs(payloads)), gw1_history=other_rows).source == "unresolved"


def test_missing_player_in_permanent_snapshot_contradicts_continuous_ownership(payloads):
    payloads["latest_picks"]["picks"][0]["element"] = 20
    result = price(parse_manager_import(inputs(payloads)))
    assert result.source == "unresolved"
    assert "published_picks_conflict_with_ownership_history" in result.reasons


def test_temporary_snapshot_absence_does_not_contradict_permanent_ownership(payloads):
    chip(payloads, 5)
    payloads["latest_picks"]["active_chip"] = "freehit"
    payloads["latest_picks"]["picks"][0]["element"] = 20
    assert price(parse_manager_import(inputs(payloads))).source == "assumed held since GW1"


def test_gw1_absence_cannot_be_repaired_by_a_later_snapshot_alone(payloads):
    payloads["gw1_picks"]["picks"][0]["element"] = 20
    result = price(parse_manager_import(inputs(payloads)))
    assert result.source == "unresolved"
    assert not result.checks.current_and_gw1_ownership
