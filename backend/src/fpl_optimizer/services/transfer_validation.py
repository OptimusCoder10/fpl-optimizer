"""Independent transfer checks; no solver, model builder or orchestration imports."""

from collections import Counter

from fpl_optimizer.schemas.transfers import TransferInputs, TransferSolution
from fpl_optimizer.services.optimization_validation import validate_lineup_solution


def validate_transfer_solution(
    inputs: TransferInputs, solution: TransferSolution, *, scenario: str,
    fixed_score: int | None = None,
) -> None:
    players = {p.player_id: p for p in inputs.players}
    owned = {p.player_id: p.selling_price_tenths for p in inputs.owned}
    selected = set(solution.squad)
    if (len(solution.squad) != 15 or len(selected) != 15
            or any(type(i) is not int or i not in players for i in selected)):
        raise ValueError("invalid selected squad")
    selected_players = tuple(players[i] for i in solution.squad)
    if Counter(p.position for p in selected_players) != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
        raise ValueError("invalid squad positions")
    incoming, outgoing = selected - owned.keys(), owned.keys() - selected
    if (tuple(sorted(incoming)) != solution.bought or tuple(sorted(outgoing)) != solution.sold
            or any(type(i) is not int for i in (*solution.bought, *solution.sold))):
        raise ValueError("transfer lists do not match ownership")
    if (type(solution.transfer_count) is not int or solution.transfer_count != len(incoming)
            or len(incoming) != len(outgoing)):
        raise ValueError("invalid transfer count")
    if not set(inputs.locked_ids) <= selected or set(inputs.excluded_ids) & selected:
        raise ValueError("violated lock or exclusion")
    if any(price is None and i not in selected for i, price in owned.items()):
        raise ValueError("unresolved selling price must retain player")
    if scenario == "hold":
        if selected != owned.keys() or solution.transfer_count != 0:
            raise ValueError("hold must preserve ownership")
    elif scenario == "transfers":
        if not 1 <= solution.transfer_count <= inputs.free_transfers:
            raise ValueError("outside remaining free transfers")
        if inputs.transfers_made + solution.transfer_count > 20:
            raise ValueError("ordinary transfer cap exceeded")
        if any(count > 3 for count in Counter(p.club_id for p in selected_players).values()):
            raise ValueError("transfer squad exceeds club limit")
    else:
        raise ValueError("unsupported scenario")
    for i in incoming:
        player = players[i]
        if (player.season_id != inputs.season_id or not player.can_select
                or not player.can_transact or player.removed
                or player.score_m is None or not player.availability_resolved):
            raise ValueError("ineligible purchase")
    bank = (inputs.bank_tenths + sum(owned[i] for i in outgoing)
            - sum(players[i].current_price_tenths for i in incoming))
    if (type(solution.remaining_bank_tenths) is not int
            or solution.remaining_bank_tenths != bank or bank < 0):
        raise ValueError("invalid remaining bank")
    validate_lineup_solution(tuple(p.lineup_player() for p in selected_players), solution.lineup)
    if fixed_score is not None and solution.lineup.score_units != fixed_score:
        raise ValueError("proven primary score was not preserved")
