"""Chain the slice 1.5 saved archive reader through 2.3 and 2.4."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete

from fpl_optimizer.db.models import Season
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.schemas.bootstrap import SharedPublication
from fpl_optimizer.schemas.prior_archive import (
    ARCHIVE_RULES, ARCHIVE_SEASON, PINNED_SHA256, validate_archive_files,
)
from fpl_optimizer.services.prediction_engine import predict
from fpl_optimizer.services.prior_archive import publish_prior_archive, read_prior_inputs
from fpl_optimizer.services.prior_selection import select_prior
from fpl_optimizer.services.shared_catalog import publish_shared_catalog


SAMPLE = Path(__file__).parent / "fixtures" / "prior_archive"


@pytest.mark.database
async def test_saved_prior_archive_reader_select_prior_and_predict_chain(
    migrated_test_engine, publication_settings,
):
    factory = create_session_factory(migrated_test_engine)
    async with factory.begin() as session:
        await session.execute(delete(Season).where(Season.id.in_([ARCHIVE_SEASON, "2026-27"])))
    try:
        files = {name: (SAMPLE / name).read_bytes() for name in PINNED_SHA256}
        # The final sample row deliberately conflicts with the earlier copy.
        files["merged_gw.csv"] = b"".join(files["merged_gw.csv"].splitlines(keepends=True)[:-1])
        archive = validate_archive_files(files)
        publication = SharedPublication(
            season_id=ARCHIVE_SEASON, rules_version=ARCHIVE_RULES,
            source_observed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            published_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        )
        await publish_prior_archive(factory, archive, publication,
                                    settings=publication_settings, allow_sample=True)
        catalog = deepcopy(archive.catalog)
        old = next(player for player in catalog.bootstrap.elements if player.id == 817)
        catalog.bootstrap.elements = [old.model_copy(update={
            "id": 42, "element_type": 4, "web_name": "Current player",
        })]
        await publish_shared_catalog(
            factory, catalog, publication.model_copy(update={
                "season_id": "2026-27", "rules_version": "fpl-2026-v1",
            }), settings=publication_settings,
        )
        inputs = await read_prior_inputs(factory, "2026-27", 42, allow_sample=True)
        prior = select_prior(player_id=42, position=4, current_rows=(), previous=inputs)
        result = predict(
            personal_rows=(), prior=prior, position=4, target_fixture_ids=(901, 902),
            catalog_trusted=True, chance=None, status="a", owned=False,
            rules_version="fpl-2026-v1", input_version="saved-sample-reader",
        )
        assert inputs.personal_player_id == 817
        assert prior.source == result.prior_source == "previous_season_personal"
        assert prior.row_count == result.prior_row_count == 2
        assert prior.expected_minutes == result.expected_minutes_per_fixture == 90
        assert result.raw_sample_count == 0
        assert result.effective_sample_size == 0
        assert result.appearance_probabilities.zero_minutes == 0
        assert result.appearance_probabilities.sixty_plus == 1
        assert result.event_mean == 2 * result.per_fixture[0].mean_points
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Season).where(Season.id.in_([ARCHIVE_SEASON, "2026-27"])))
