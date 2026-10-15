"""Fetch manager-only evidence and pass transport completeness to the pure service."""

from datetime import datetime

from fpl_optimizer.schemas.manager_import import ImportInputs, ManagerSnapshot
from fpl_optimizer.services.fpl_http import FPLClient
from fpl_optimizer.services.manager_import import parse_manager_import


async def fetch_manager_import(
    client: FPLClient, *, season_id: str, entry_id: int,
    deadlines: dict[int, datetime],
) -> ManagerSnapshot:
    """Use captured catalog deadlines; never fetch shared data during an import.

    The caller supplies a client with a bounded import budget. Earlier published
    events are tried until a usable permanent snapshot is found. GW1 is reused.
    """
    entry = await client.entry(entry_id)
    history = await client.history(entry_id)
    transfers = await client.transfers(entry_id)
    picks = {}

    def snapshot():
        return parse_manager_import(ImportInputs(
            season_id=season_id, entry_id=entry_id,
            entry=entry.manager_response(), history=history.manager_response(),
            transfers=transfers.manager_response(), picks_by_event=picks,
            deadlines=deadlines, imported_at=client.clock(),
        ))

    events = sorted((event for event, deadline in deadlines.items()
                     if deadline < client.clock()), reverse=True)
    if entry.data is not None and entry.data.current_event is not None:
        events = [event for event in events if event <= entry.data.current_event]
    for event in events:
        if client.exhausted:
            break
        picks[event] = (await client.picks(entry_id, event)).manager_response()
        if snapshot().permanent_snapshot is not None:
            break
    if (history.data is not None and history.data.current
            and min(row.event for row in history.data.current) == 1
            and 1 not in picks and not client.exhausted):
        picks[1] = (await client.picks(entry_id, 1)).manager_response()
    return snapshot()
