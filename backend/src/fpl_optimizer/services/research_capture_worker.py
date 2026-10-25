"""Internal process: accepts only the parent's trusted pickled plain view."""

import gzip
import pickle
import sys

from fpl_optimizer.services.forecast_bundle import assemble_research_bundle
from fpl_optimizer.services.forecast_bundle_inputs import assemble_player_inputs
from fpl_optimizer.services.forecast_bundle_policy import label_player_history
from fpl_optimizer.services.history_eligibility import HistoryEligibilityView
from fpl_optimizer.services.research_capture import INPUT_VERSION


def main():
    view = pickle.load(sys.stdin.buffer)
    history = HistoryEligibilityView(view.shared_freshness, view.player_history,
                                     view.history_publication_version,
                                     view.shared_publication.publication_version)
    labels = {p.player_id: label_player_history(history, p, target_event_id=view.target.id,
                                               as_of=view.as_of) for p in view.player_history}
    inputs = assemble_player_inputs(view, labels)
    result = assemble_research_bundle(view, labels, inputs, input_version=INPUT_VERSION)
    pickle.dump((result.canonical_bytes, gzip.compress(result.canonical_bytes, mtime=0)), sys.stdout.buffer)


if __name__ == "__main__":
    main()
