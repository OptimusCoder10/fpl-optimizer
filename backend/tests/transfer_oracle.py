"""Small exhaustive reference; no production model/validator/eligibility helpers."""

from collections import Counter
from itertools import combinations

from lineup_oracle import exhaustive_lineup_score


def exhaustive_transfers(inputs):
    owned = {o.player_id: o.selling_price_tenths for o in inputs.owned}
    eligible = []
    for p in inputs.players:
        if p.player_id in owned or (p.season_id == inputs.season_id and p.can_select
            and p.can_transact and not p.removed and p.score_m is not None and p.availability_resolved):
            eligible.append(p)
    best = None
    for squad in combinations(eligible, 15):
        ids = {p.player_id for p in squad}
        if not set(inputs.locked_ids) <= ids or set(inputs.excluded_ids) & ids:
            continue
        if Counter(p.position for p in squad) != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
            continue
        incoming, outgoing = ids-owned.keys(), owned.keys()-ids
        n = len(incoming)
        if n != len(outgoing) or n > inputs.free_transfers or inputs.transfers_made+n > 20:
            continue
        if any(owned[i] is None for i in outgoing):
            continue
        if n and max(Counter(p.club_id for p in squad).values()) > 3:
            continue
        bank = inputs.bank_tenths + sum(owned[i] for i in outgoing)
        bank -= sum(p.current_price_tenths for p in squad if p.player_id in incoming)
        if bank < 0:
            continue
        objective = (exhaustive_lineup_score(squad), -n)
        if best is None or objective > best:
            best = objective
    return best
