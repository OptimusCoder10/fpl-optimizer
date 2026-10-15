"""Invented full-season endpoints for task 4a; no captured FPL/manager data."""

from datetime import datetime, timedelta, timezone

from fpl_optimizer.schemas.fixtures import SEASON_CLUB_COUNTS

SEASON = "2026-27"


class Clock:
    def __init__(self):
        self.time = datetime(2030, 10, 1, tzinfo=timezone.utc)
        self.elapsed = 0.0
        self.sleeps = []

    def now(self):
        return self.time

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds
        self.time += timedelta(seconds=seconds)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


def full_season(n=None):
    n = SEASON_CLUB_COUNTS[SEASON] if n is None else n
    teams = [dict(id=i, code=1000+i, name=f"Synthetic club {i}", short_name=f"C{i}",
                  strength=None, strength_overall_home=None, strength_overall_away=None,
                  strength_attack_home=None, strength_attack_away=None,
                  strength_defence_home=None, strength_defence_away=None)
             for i in range(1, n+1)]
    players = [dict(id=i, code=2000+i, team=1, first_name="Synthetic", second_name=str(i),
                    web_name=f"Player {i}", element_type=2, has_temporary_code=False,
                    opta_code=None, now_cost=50, can_select=True, can_transact=True,
                    removed=False, status="a", chance_of_playing_next_round=None,
                    **{field: None for field in ("form", "ep_next", "selected_by_percent",
                        "ict_index", "expected_goals", "expected_assists",
                        "expected_goal_involvements", "expected_goals_conceded")})
               for i in (1, 2)]
    events = [dict(id=i, name=f"Synthetic event {i}", deadline_time=deadline,
                   finished=i == 1, data_checked=i == 1, is_current=False, is_next=i == 2)
              for i, deadline in [(1, "2030-09-01T12:00:00Z"), (2, "2030-11-01T12:00:00Z")]]
    fixtures = [dict(id=index, code=3000+index, event=1,
                     kickoff_time="2030-09-01T15:00:00Z", started=True, finished=True,
                     finished_provisional=True, team_h=home, team_a=away,
                     team_h_score=0, team_a_score=0, team_h_difficulty=3, team_a_difficulty=3)
                for index, (home, away) in enumerate(
                    ((h, a) for h in range(1, n+1) for a in range(1, n+1) if h != a), 1)]
    return dict(teams=teams, elements=players, events=events), fixtures


def history(player_id):
    counts = ("minutes", "goals_scored", "assists", "clean_sheets", "goals_conceded",
              "own_goals", "penalties_saved", "penalties_missed", "yellow_cards",
              "red_cards", "saves", "bonus", "bps", "clearances_blocks_interceptions",
              "recoveries", "tackles", "defensive_contribution", "starts", "selected",
              "transfers_in", "transfers_out", "transfers_balance")
    decimals = ("influence", "creativity", "threat", "ict_index", "expected_goals",
                "expected_assists", "expected_goal_involvements", "expected_goals_conceded")
    return dict(fixtures=[], history_past=[], history=[dict(
        element=player_id, fixture=1, opponent_team=2, total_points=0, was_home=True,
        kickoff_time="2030-09-01T15:00:00Z", team_h_score=0, team_a_score=0,
        round=1, modified=False, value=50,
        **dict.fromkeys(counts, 0), **dict.fromkeys(decimals, "0.0"),
    )])
