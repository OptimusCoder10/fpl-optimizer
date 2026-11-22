"""Independent arithmetic/legality checks: no PuLP or model-builder imports."""

from collections import Counter
from collections.abc import Sequence

from fpl_optimizer.schemas.optimization import LineupPlayer, LineupSolution


def validate_lineup_solution(
    players: Sequence[LineupPlayer], solution: LineupSolution,
) -> None:
    """Raise ValueError on invalid output; do not attempt to repair it."""
    owned = {p.player_id: p for p in players}
    if len(players) != 15 or len(owned) != 15:
        raise ValueError("invalid owned squad")
    if Counter(p.position for p in players) != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
        raise ValueError("invalid owned positions")
    xi = solution.starting_xi
    if len(xi) != 11 or len(set(xi)) != 11:
        raise ValueError("XI must contain eleven distinct players")
    all_ids = (*xi, solution.captain, solution.vice_captain,
               *solution.outfield_bench, solution.reserve_goalkeeper)
    if any(type(i) is not int or i not in owned for i in all_ids):
        raise ValueError("solution contains an unowned or invalid ID")
    counts = Counter(owned[i].position for i in xi)
    if not (counts["GK"] == 1 and 3 <= counts["DEF"] <= 5
            and 2 <= counts["MID"] <= 5 and 1 <= counts["FWD"] <= 3):
        raise ValueError("invalid XI formation")
    if solution.captain not in xi:
        raise ValueError("captain must start")
    if type(solution.score_units) is not int:
        raise ValueError("objective must be integer units")
    expected = sum(owned[i].score_m for i in xi) + owned[solution.captain].score_m
    if solution.score_units != expected:
        raise ValueError("objective does not match XI plus captain")
    bench = (*solution.outfield_bench, solution.reserve_goalkeeper)
    if len(solution.outfield_bench) != 3 or len(set(bench)) != 4:
        raise ValueError("bench must contain three outfield players and a goalkeeper")
    if set(bench) != set(owned) - set(xi):
        raise ValueError("bench must be the remaining owned players")
    if (owned[solution.reserve_goalkeeper].position != "GK"
            or any(owned[i].position == "GK" for i in solution.outfield_bench)):
        raise ValueError("reserve goalkeeper must be separate")

    # Recompute the declared heuristic independently of the engine's helper.
    def order(player_id: int):
        player = owned[player_id]
        appearance = player.appearance_probability
        return (-player.score_m, appearance is None,
                -appearance if appearance is not None else 0, player_id)

    if solution.vice_captain != min((i for i in xi if i != solution.captain), key=order):
        raise ValueError("invalid vice-captain heuristic")
    if solution.outfield_bench != tuple(sorted(solution.outfield_bench, key=order)):
        raise ValueError("invalid bench-order heuristic")
