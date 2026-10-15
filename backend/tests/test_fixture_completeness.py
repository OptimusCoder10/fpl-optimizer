"""Approved J33 rule, exercised only with invented seasons."""

import pytest
from pydantic import ValidationError

from fpl_optimizer.schemas.bootstrap import BootstrapEnvelope
from fpl_optimizer.schemas.fixtures import (
    FixturesEnvelope, IncompleteFixtures, SEASON_CLUB_COUNTS, validate_full_season,
)
from synthetic_fpl import SEASON, full_season


def validate(bootstrap, fixtures):
    return validate_full_season(SEASON, BootstrapEnvelope.model_validate(bootstrap),
                                FixturesEnvelope.model_validate(fixtures))


def test_valid_full_season_passes():
    b, f = full_season()
    assert len(validate(b, f).fixtures.root) == len(b["teams"]) * (len(b["teams"])-1)


def test_one_fixture_short_fails():
    b, f = full_season()
    with pytest.raises(IncompleteFixtures):
        validate(b, f[:-1])


def test_right_total_but_duplicated_and_missing_pairing_fails():
    b, f = full_season()
    f[-1].update(team_h=f[0]["team_h"], team_a=f[0]["team_a"])
    with pytest.raises(IncompleteFixtures):
        validate(b, f)


def test_club_playing_itself_fails():
    b, f = full_season()
    f[0]["team_a"] = f[0]["team_h"]
    with pytest.raises(ValidationError):
        validate(b, f)


@pytest.mark.parametrize("offset", [-1, 1])
def test_n_minus_or_plus_one_clubs_fail(offset):
    with pytest.raises(IncompleteFixtures):
        validate(*full_season(SEASON_CLUB_COUNTS[SEASON] + offset))


def test_duplicate_fixture_ids_fail():
    b, f = full_season()
    f[-1]["id"] = f[0]["id"]
    with pytest.raises(ValidationError):
        validate(b, f)


def test_null_event_and_kickoff_allowed():
    b, f = full_season()
    f[0].update(event=None, kickoff_time=None)
    assert validate(b, f).fixtures.root[0].event is None
