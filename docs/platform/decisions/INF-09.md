# INF-09 – test fixes so that the gate steps run reliably in a Linux VM

The work-package tools run `platform-web`, `platform-e2e` and `platform-contracts` inside a Linux VM with fixed vCPUs on
the large test device (fixed resources per run, no overbooking). Four tests depended on timing that differs between
macOS and Linux; they now wait for the condition or cover the branch deterministically. No product code changed.

1. `web/tests/core/store/reader.test.ts`: three cases waited a fixed 20 event-loop turns (`settle()`) for WebCrypto
   work that needs more turns on Linux. They now wait for the expected state (`vi.waitFor`, 5 s limit, 10 ms interval).
2. `e2e/platform/tests/test_control.py::test_discovery`: creates three packages in quick succession; on file systems
   with coarse timestamps the import system's directory cache missed the third. `make_package` now calls
   `importlib.invalidate_caches()`.
3. `web/tests/ui/joined.test.tsx` (new): the neutral org-name fallback of the membership notice was only reached by a
   browser spec when the join won a race against loading the org directory. On Linux it lost, so merged web coverage
   fell below 100 %. A unit test covers the branch, and the notice showing only in the joined org.

Not done here: `platform-test` keeps running natively (22 tests need macOS: keychain via `security`, daemon signals,
Lima sampler, transcript). Splitting them out, with coverage combined from the VM and the device, is proposed in the
work-package report.
