"""Pure fixture scoring checks from §04D and §08B."""

from dataclasses import asdict
import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace

from fpl_optimizer.schemas.element_summary import ElementSummaryEnvelope
from fpl_optimizer.schemas.prior_archive import PINNED_SHA256, validate_archive_files
from fpl_optimizer.services import player_history
from fpl_optimizer.services.scoring_components import score_fixture


FIXTURES = Path(__file__).parent / "fixtures"
COUNTS = {
    "minutes": 0,
    "goals_scored": 0,
    "assists": 0,
    "clean_sheets": 0,
    "saves": 0,
    "goals_conceded": 0,
    "clearances_blocks_interceptions": 0,
    "tackles": 0,
    "recoveries": 0,
    "bonus": 0,
    "penalties_saved": 0,
    "penalties_missed": 0,
    "yellow_cards": 0,
    "red_cards": 0,
    "own_goals": 0,
}


def scored(position=2, **changes):
    return score_fixture(SimpleNamespace(**(COUNTS | changes)), position)


def test_appearance_minutes_0_1_59_60():
    assert [scored(minutes=m).appearance for m in (0, 1, 59, 60)] == [0, 1, 1, 2]


def test_goal_and_assist_points_by_position():
    assert [(points.goals, points.assists)
            for points in (scored(position=p, minutes=60, goals_scored=1, assists=1)
                           for p in (1, 2, 3, 4))] == [(10, 3), (6, 3), (5, 3), (4, 3)]


def test_clean_sheet_qualification_by_position():
    for position, expected in ((1, 4), (2, 4), (3, 1), (4, 0)):
        assert scored(position, minutes=59, clean_sheets=1).clean_sheet == 0
        assert scored(position, minutes=60, clean_sheets=1).clean_sheet == expected


def test_saves_2_3_5_6_and_conceded_1_2_3_4():
    assert [scored(1, saves=n).saves for n in (2, 3, 5, 6)] == [0, 1, 1, 2]
    for position in (1, 2):
        assert [scored(position, goals_conceded=n).conceded
                for n in (1, 2, 3, 4)] == [0, -1, -1, -2]
    assert scored(2, saves=6).saves == 0
    assert scored(3, goals_conceded=4).conceded == 0
    assert scored(4, goals_conceded=4).conceded == 0


def test_defensive_contribution_below_at_above_by_position():
    assert [scored(1, clearances_blocks_interceptions=n).defensive_contribution
            for n in (9, 10, 11, 12, 13)] == [0, 0, 0, 0, 0]
    assert [scored(2, clearances_blocks_interceptions=8, tackles=n).defensive_contribution
            for n in (1, 2, 3)] == [0, 2, 2]
    for position in (3, 4):
        assert [scored(position, clearances_blocks_interceptions=8, tackles=2,
                       recoveries=n).defensive_contribution
                for n in (1, 2, 3)] == [0, 2, 2]
    assert scored(2, clearances_blocks_interceptions=8, tackles=1,
                  recoveries=99).defensive_contribution == 0


def test_penalties_cards_and_own_goals():
    points = scored(penalties_saved=1, penalties_missed=1, yellow_cards=1,
                    red_cards=1, own_goals=1, bonus=3)
    assert (points.penalty_saves, points.penalty_misses, points.yellow_cards,
            points.red_cards, points.own_goals, points.bonus) == (5, -2, -1, -3, -2, 3)


def test_zero_minute_card_record():
    points = scored(minutes=0, yellow_cards=1)
    assert points.appearance == 0
    assert points.yellow_cards == -1
    assert points.total_points == -1


def test_zero_total_points_does_not_mean_did_not_play():
    row = SimpleNamespace(**(COUNTS | {"minutes": 1, "yellow_cards": 1,
                                     "total_points": 0}))
    points = score_fixture(row, 2)
    assert (points.appearance, points.yellow_cards, points.total_points) == (1, -1, 0)


def test_saved_samples_components_sum_to_recorded_total():
    live = ElementSummaryEnvelope.model_validate(json.loads(
        (FIXTURES / "element_summary_5.json").read_text()))
    sample = FIXTURES / "prior_archive"
    files = {name: (sample / name).read_bytes() for name in PINNED_SHA256}
    # The saved CSV deliberately includes one conflicting variant for quarantine tests.
    records = list(csv.DictReader(io.StringIO(files["merged_gw.csv"].decode())))
    records.pop()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(records[0]))
    writer.writeheader()
    writer.writerows(records)
    files["merged_gw.csv"] = output.getvalue().encode()
    archive = validate_archive_files(files)
    rows = [(row, 2) for row in live.history]
    rows.extend((row, next(player.element_type for player in archive.catalog.bootstrap.elements
                           if player.id == player_id))
                for player_id, envelope in archive.histories.items()
                for row in envelope.history)
    assert rows
    for row, position in rows:
        components = score_fixture(row, position)
        assert sum(asdict(components).values()) == components.total_points == row.total_points


def test_reconciliation_calls_pure_scoring_function(monkeypatch):
    row = SimpleNamespace(**(COUNTS | {"minutes": 60, "total_points": 2}))
    calls = []
    actual = score_fixture

    def spy(input_row, position):
        calls.append((input_row, position))
        return actual(input_row, position)

    monkeypatch.setattr(player_history, "score_fixture", spy)
    assert player_history._reconstruct_fixture_points(row, 2) == 2
    assert calls == [(row, 2)]


def test_optional_fields_remain_null():
    row = SimpleNamespace(**(COUNTS | {"minutes": 60, "starts": None,
                                     "expected_goals": None, "ict_index": None}))
    assert score_fixture(row, 2).appearance == 2
    assert (row.starts, row.expected_goals, row.ict_index) == (None, None, None)
