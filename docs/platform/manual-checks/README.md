# Manual checks per release

One file per release version, `<version>.md` ([06 §12.7](../spec/06-web.md#127-browser-support-and-manual-checklist),
09-milestones.md WP-M3-06, ADR-006): the PRF device matrix with the rows the automated suite runs (filled in by the
run) and the rows that need real devices and a person (open until checked by hand).

| Release | File | State |
|---|---|---|
| 0.1.0 (integration branch, M3) | [0.1.0.md](0.1.0.md) | automated rows passed; manual rows open |

Procedure for the real-device rows, valid for every release: [prf-devices.md](prf-devices.md) (one person, one
device, at most 10 minutes per row; KOR-54).
