"""Offline CSV adapter for the pinned fixture archive; no network access."""

from collections import defaultdict
from dataclasses import asdict, dataclass
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import get_args

from fpl_optimizer.schemas.bootstrap import (
    BootstrapEnvelope, BootstrapGameweek, BootstrapPlayer, BootstrapTeam,
)
from fpl_optimizer.schemas.element_summary import ElementFixtureHistory, ElementSummaryEnvelope
from fpl_optimizer.schemas.fixtures import FPLFixture, FixturesEnvelope, SharedCatalogEnvelope
from fpl_optimizer.services.player_history import _reconstruct_fixture_points

ARCHIVE_SEASON = "2025-26"
ARCHIVE_COMMIT = "9779cdbc0c07f6c900c2d0c181ddf6bb9c800f88"
ARCHIVE_RULES = "fpl-2025-v1"
SCORING_ADAPTER = "fpl-defensive-contribution-v1"
PINNED_SHA256 = {
    "merged_gw.csv": "0d09f1f1cb1b5520ec8e2f25238aa652efe2a263d8ca7cb2b6538b27bf86727d",
    "players_raw.csv": "412ce0172016f8f98f25177dc6de9f3cd2a8ec7a6135f9aa638d7fdee784d67b",
    "teams.csv": "b29df099cb0ad25413e284e53116099b0e0496874f99743dbc0870d8241b46c5",
    "fixtures.csv": "2d7e3950d346df14ca486cb09e9b9ba406d37d943775244eed06cdc021ffb3a9",
}
POSITIONS = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}


@dataclass(frozen=True)
class ArchiveAudit:
    rows: int
    exact_duplicates: int
    distinct_rows: int
    distinct_keys: int
    conflicting_keys: tuple[tuple[int, int], ...]
    multi_fixture_player_events: int
    players_without_history: tuple[int, ...] = ()
    reconciled_rows: int = 0


@dataclass(frozen=True)
class QuarantinedRow:
    line: int
    key: tuple[int, int]
    source: dict[str, str]


class ArchiveConflictError(ValueError):
    """All variants of conflicting keys are withheld, with raw evidence intact."""

    def __init__(self, audit: ArchiveAudit, quarantine: tuple[QuarantinedRow, ...]):
        super().__init__(f"conflicting archive player-fixture keys: {audit.conflicting_keys}")
        self.audit = audit
        self.quarantine = quarantine

    def write_quarantine(self, path: Path) -> None:
        """Save the rejected rows and audit outside the operational tables."""
        path.write_text(json.dumps({
            "audit": asdict(self.audit),
            "quarantine": [asdict(row) for row in self.quarantine],
        }, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class ValidatedArchive:
    catalog: SharedCatalogEnvelope
    histories: dict[int, ElementSummaryEnvelope]
    audit: ArchiveAudit
    provenance: dict


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("missing or duplicate CSV headers")
    rows = list(reader)
    if not rows or any(None in row or None in row.values() for row in rows):
        raise ValueError("empty or malformed CSV rows")
    return rows


def _value(value: str, annotation: object) -> object:
    if value in {"", "None"}:
        return None
    # Strict model validation follows this source-format conversion.
    types = get_args(annotation) or (annotation,)
    if bool in types:
        if value not in {"True", "False"}:
            raise ValueError("CSV boolean must be True or False")
        return value == "True"
    if int in types:
        return int(value)
    return value


def _parse_model(model, row: dict[str, str]):
    return model.model_validate({
        name: _value(row.get(name, ""), field.annotation)
        for name, field in model.model_fields.items()
        if name in row or type(None) in get_args(field.annotation)
    })


def _one_to_one(rows, label: str) -> None:
    codes = [row.code for row in rows]
    if len(codes) != len(set(codes)):
        raise ValueError(f"{label} external codes are not one-to-one")


def load_pinned_archive(directory: Path) -> ValidatedArchive:
    """Load only the four exact pinned files, verified before CSV parsing."""
    files = {name: (directory / name).read_bytes() for name in PINNED_SHA256}
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if checksums != PINNED_SHA256:
        raise ValueError("pinned archive checksum mismatch")
    archive = validate_archive_files(files, source_kind="pinned")
    audit = archive.audit
    if (audit.rows, audit.exact_duplicates, audit.distinct_keys,
        audit.conflicting_keys, audit.multi_fixture_player_events, audit.reconciled_rows
        ) != (29757, 10, 29747, (), 409, 29747):
        raise ValueError(f"pinned archive audit differs from specification: {audit}")
    return archive


def validate_archive_files(files: dict[str, bytes], *, source_kind: str = "sample") -> ValidatedArchive:
    """Validate supplied bytes. Sample provenance cannot masquerade as the full pin."""
    if set(files) != set(PINNED_SHA256):
        raise ValueError("archive requires merged_gw, players_raw, teams and fixtures CSVs")
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if source_kind not in {"sample", "pinned"}:
        raise ValueError("unknown archive source kind")
    if source_kind == "pinned" and checksums != PINNED_SHA256:
        raise ValueError("pinned archive checksum mismatch")

    raw = _csv_rows(files["merged_gw.csv"])
    unique: dict[tuple, tuple[int, dict]] = {}
    for line, row in enumerate(raw, 2):
        unique.setdefault(tuple(row.items()), (line, row))
    variants: dict[tuple[int, int], list[QuarantinedRow]] = defaultdict(list)
    events: dict[tuple[int, int], set[int]] = defaultdict(set)
    for line, row in unique.values():
        key = (int(row["element"]), int(row["fixture"]))
        variants[key].append(QuarantinedRow(line, key, row))
        events[(key[0], int(row["round"]))].add(key[1])
    conflicts = tuple(sorted(key for key, rows in variants.items() if len(rows) > 1))
    audit = ArchiveAudit(
        rows=len(raw), exact_duplicates=len(raw) - len(unique), distinct_rows=len(unique),
        distinct_keys=len(variants), conflicting_keys=conflicts,
        multi_fixture_player_events=sum(len(ids) > 1 for ids in events.values()),
    )
    if conflicts:
        raise ArchiveConflictError(audit, tuple(
            row for key in conflicts for row in variants[key]
        ))

    teams = [_parse_model(BootstrapTeam, row) for row in _csv_rows(files["teams.csv"])]
    raw_players = _csv_rows(files["players_raw.csv"])
    players = [_parse_model(BootstrapPlayer, row) for row in raw_players]
    fixtures = [_parse_model(FPLFixture, row) for row in _csv_rows(files["fixtures.csv"])]
    _one_to_one(teams, "team")
    _one_to_one(players, "player")
    _one_to_one(fixtures, "fixture")
    team_codes = {team.id: team.code for team in teams}
    for raw_player, player in zip(raw_players, players, strict=True):
        if int(raw_player["team_code"]) != team_codes.get(player.team):
            raise ValueError(f"player {player.id} team external-code join failed")

    # Fixtures do not archive official deadlines or event data_checked evidence.
    # Keep them unknown rather than manufacturing live finalization observations.
    events = [BootstrapGameweek(
        id=event, name=f"Gameweek {event}", deadline_time=None,
        is_current=False, is_next=False, finished=all(
            fixture.finished for fixture in fixtures if fixture.event == event
        ), data_checked=False,
    ) for event in sorted({fixture.event for fixture in fixtures if fixture.event is not None})]
    catalog = SharedCatalogEnvelope(
        bootstrap=BootstrapEnvelope(teams=teams, elements=players, events=events),
        fixtures=FixturesEnvelope(fixtures),
    )
    player_by_id = {player.id: player for player in players}
    fixture_by_id = {fixture.id: fixture for fixture in fixtures}
    grouped = defaultdict(list)
    for _, raw_row in unique.values():
        row = _parse_model(ElementFixtureHistory, raw_row)
        player = player_by_id.get(row.element)
        fixture = fixture_by_id.get(row.fixture)
        if player is None or fixture is None:
            raise ValueError(f"archive row {(row.element, row.fixture)} has unknown season identity")
        if POSITIONS.get(raw_row["position"]) != player.element_type:
            raise ValueError(f"archive player {player.id} position evidence conflicts")
        if int(raw_row["GW"]) != row.round or fixture.event != row.round:
            raise ValueError(f"archive fixture {fixture.id} gameweek conflicts")
        if fixture.kickoff_time != row.kickoff_time:
            raise ValueError(f"archive fixture {fixture.id} kickoff conflicts")
        opponent = fixture.team_a if row.was_home else fixture.team_h
        if opponent != row.opponent_team:
            raise ValueError(f"archive fixture {fixture.id} sides conflict")
        if _reconstruct_fixture_points(row, player.element_type) != row.total_points:
            raise ValueError(f"archive row {(row.element, row.fixture)} total_points mismatch")
        grouped[row.element].append(row)
    histories = {player_id: ElementSummaryEnvelope(fixtures=[], history=rows, history_past=[])
                 for player_id, rows in grouped.items()}
    audit = ArchiveAudit(**{
        **asdict(audit), "players_without_history": tuple(sorted(player_by_id.keys() - histories.keys())),
        "reconciled_rows": len(unique),
    })
    provenance = {
        "source": "vaastav/Fantasy-Premier-League", "season": ARCHIVE_SEASON,
        "commit": ARCHIVE_COMMIT, "kind": source_kind, "sha256": checksums,
        "scoring_adapter": SCORING_ADAPTER, "audit": asdict(audit),
    }
    # JSON round trip keeps persisted and in-memory provenance equality stable.
    provenance = json.loads(json.dumps(provenance))
    return ValidatedArchive(catalog, histories, audit, provenance)
