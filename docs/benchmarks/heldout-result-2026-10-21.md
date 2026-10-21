# One-time held-out comparison — 21 October 2026

The frozen 12/6/3 baseline and frozen opponent setting (L=12, λ=12) were compared once on GW20–38. The predeclared benchmark rule adopts the opponent candidate only if **both** overall and active-cohort MAE are strictly lower. The two models had identical cohorts: 15,498 accepted player-events overall and 5,997 active. All 19 events were scored; none was skipped. There were 13 schedule-mismatch exclusions and 468 first-time players excluded before evaluation.

| Metric | Baseline | Opponent |
|---|---:|---:|
| Overall MAE | 1.092133 | 1.090439 |
| Active MAE | 1.984846 | 1.978735 |
| Overall RMSE | 1.946329 | 1.942779 |
| Active RMSE | 2.899507 | 2.893196 |
| Overall signed bias | +0.048167 | +0.048789 |
| Active signed bias | −0.358594 | −0.358813 |
| Overall binary appearance Brier | 0.107521 | 0.107521 |
| Overall three-category minute Brier | 0.146008 | 0.146008 |
| Active binary appearance Brier | 0.163035 | 0.163035 |
| Active three-category minute Brier | 0.254311 | 0.254311 |

| Position MAE | Baseline | Opponent |
|---|---:|---:|
| GK | 0.711103 | 0.710072 |
| DEF | 1.247292 | 1.244070 |
| MID | 1.051332 | 1.050525 |
| FWD | 1.197541 | 1.196038 |

The paired weekly opponent-minus-baseline MAE difference was −0.001662, with standard error 0.001266 across 19 weeks. The sole neutral reason was `under_four_player_observations` (204). **Benchmark verdict: opponent adopted by the declared rule**, because both MAEs are strictly lower.

The effect is small: overall MAE changed by about −0.0017 points (about 0.16%) and active MAE by about −0.0061. The weekly paired difference is about 1.3 standard errors from zero, suggestive but not conclusive. The gate is pragmatic, not a significance test. This is a benchmark verdict only: the first release stays on the baseline, the live predictor is unchanged, and enabling opponent adjustment live is a separate future slice.

This retrospective benchmark uses final archived fixture assignments and corrected results, with availability held neutral. The active-player signed bias of about −0.36 remains unaddressed. It makes no claim of beating human managers.

Aggregate evidence: [heldout-joint-comparison-2026-10-21.json](heldout-joint-comparison-2026-10-21.json), SHA-256 `f305147d1fe87ae8e6008221c31a9d700489689612f5a8514250b0c74e30900e`.
