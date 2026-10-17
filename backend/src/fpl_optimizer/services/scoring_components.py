"""Pure §04D scoring for one observed player-fixture row."""

from dataclasses import dataclass
from typing import Protocol


class FixtureScoringCounts(Protocol):
    """Required scoring counts supplied by validated history or stored rows."""

    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    saves: int
    goals_conceded: int
    clearances_blocks_interceptions: int
    tackles: int
    recoveries: int
    bonus: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    own_goals: int


@dataclass(frozen=True)
class ScoringComponents:
    appearance: int
    goals: int
    assists: int
    clean_sheet: int
    saves: int
    conceded: int
    defensive_contribution: int
    bonus: int
    penalty_saves: int
    penalty_misses: int
    yellow_cards: int
    red_cards: int
    own_goals: int

    @property
    def total_points(self) -> int:
        return sum(vars(self).values())


def score_fixture(row: FixtureScoringCounts, position: int) -> ScoringComponents:
    """Break down an actual fixture outcome; zero total never implies zero minutes."""
    appearance = 0 if row.minutes == 0 else (1 if row.minutes < 60 else 2)
    goal_points = {1: 10, 2: 6, 3: 5, 4: 4}[position]
    clean_sheet_points = {1: 4, 2: 4, 3: 1, 4: 0}[position]
    defensive_points = 0
    if position == 2:
        defensive_points = (
            2 if row.clearances_blocks_interceptions + row.tackles >= 10 else 0
        )
    elif position in {3, 4}:
        defensive_points = (
            2
            if row.clearances_blocks_interceptions + row.tackles + row.recoveries >= 12
            else 0
        )

    return ScoringComponents(
        appearance=appearance,
        goals=row.goals_scored * goal_points,
        assists=row.assists * 3,
        clean_sheet=(row.clean_sheets * clean_sheet_points if row.minutes >= 60 else 0),
        saves=row.saves // 3 if position == 1 else 0,
        conceded=-(row.goals_conceded // 2) if position in {1, 2} else 0,
        defensive_contribution=defensive_points,
        bonus=row.bonus,
        penalty_saves=row.penalties_saved * 5,
        penalty_misses=-row.penalties_missed * 2,
        yellow_cards=-row.yellow_cards,
        red_cards=-row.red_cards * 3,
        own_goals=-row.own_goals * 2,
    )
