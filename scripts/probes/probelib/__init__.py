"""Evaluation library of the early-measurement probes (PRB-01, docs/platform/spec/09-milestones.md WP-M0-14).

Pure functions only (no VM, no network): distributions, the step breakdown of the clone and pool paths against the
budgets of 05-runner.md section 6.1, the go/no-go rule, CSV and Markdown output, and the LIVE-03 helpers (profile
snapshots, token extraction without refresh, secret scan). The probe scripts next to this package do the I/O and
are run by hand on the runner device; this package is covered by `make platform-ci` (tests/platform/probes/).
"""
