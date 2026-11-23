# PRB-01 acceptance (early measurements, WP-M0-14)

| Criterion | Evidence |
|---|---|
| Clone path (10 runs, pool empty) and pool path (10 runs) end to end with timestamps per step, distribution against the budgets of 05 §6.1 | `docs/platform/measurements/M0.md` (tables), raw data `m0-clone-path.csv`; probe `scripts/probes/probe_clone_path.py` |
| TUI start under Landlock, 10 runs with and without Landlock | `M0.md` (TUI section), raw data `m0-tui-start.csv`; probes `scripts/probes/probe_tui_start.py`, `tui_guest.py` |
| LIVE-03 once: read-only access token, gateway mini turn, one usage-endpoint GET, profile unchanged (hash comparison), secret scan | `M0.md` (LIVE-03 section); probe `scripts/probes/probe_live03.py` (`snapshot`, `check`, `token`, `run`, `compare`, `scan`) |
| Go/No-Go for the 10 s target of M1 | Go: clone path p95 6.32 s ≤ 9 s (`M0.md`, ADR-029) |
| ADR addendum | `docs/platform/adr/029-m0-measurements-and-live03.md` (ADR-009: values; ADR-010: LIVE-03) |
| Unit tests of the evaluation 100 % in `make platform-ci` | `tests/platform/probes/` (stats, timeline, report, live03) over `scripts/probes/probelib/`, coverage source `scripts/probes/probelib` |
| Hygiene | `tests/platform/test_hygiene.py` scans `scripts/` and `docs/platform/` (no host names, profile names, tokens) |
