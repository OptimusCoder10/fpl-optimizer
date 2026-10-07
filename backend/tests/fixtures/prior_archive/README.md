# Saved archive sample

Source: `vaastav/Fantasy-Premier-League`, commit
`9779cdbc0c07f6c900c2d0c181ddf6bb9c800f88`, season `2025-26`.
The four CSVs retain complete source rows and headers for two players, three
teams and two fixtures. The full files are deliberately kept outside this repository.

`merged_gw.csv` contains five rows:

- Adam Armstrong (817), fixtures 257 and 310, a genuine GW26 double.
- Nathan Fraser (658), fixture 257, a genuine zero-minute row.
- An exact copy of Armstrong's first row, added for deduplication coverage.
- A copy of Fraser's row with only `xP` changed to `999`, added to prove that
  conflicting keys fail even when the differing column is excluded from inputs.

Tests remove the last row for a valid import; they retain it for quarantine checks.
Sample checksums are computed over the supplied bytes and provenance explicitly
says `sample`. `load_pinned_archive` rejects these reduced files: it requires all
four full-file SHA-256 checksums from the pinned commit.
