"""Exhaustive reference: deliberately independent of PuLP and engine helpers."""

from collections import Counter
from itertools import combinations


def exhaustive_lineup_score(players):
    best = None
    for xi in combinations(players, 11):
        positions = Counter(p.position for p in xi)
        if not (positions["GK"] == 1 and 3 <= positions["DEF"] <= 5
                and 2 <= positions["MID"] <= 5 and 1 <= positions["FWD"] <= 3):
            continue
        xi_score = sum(p.score_m for p in xi)
        for captain in xi:
            score = xi_score + captain.score_m
            if best is None or score > best:
                best = score
    if best is None:
        raise ValueError("no legal XI and captain")
    return best
