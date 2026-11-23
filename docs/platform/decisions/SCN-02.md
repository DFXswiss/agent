# SCN-02 – decisions (agent side)

1. **TEST_MODE goldens keep the Lima default user's sudo** (ADR-036): `agent-guest-harden.service` removes
   `/etc/sudoers.d/91-agent-guest-test` on every boot and writes it again only when `/var/lib/agent-golden/install.json`
   says `"test_mode": true` (rule checked with `visudo -c`). Production goldens are unchanged. The E2E suite of
   agent-core needs it for its test hooks (typing into a pane, reading the recorder file, reads as a session user) and
   the shared golden caches are now built as TEST_MODE goldens (`--test-model-port 9`).
2. **Review**: the grant of the previous boot is removed before any exit path of the hardening script; the TEST_MODE
   decision is a named check (`TEST_MODE_CHECK`) that a unit test runs against install records (true, false, string,
   missing key, invalid JSON, missing file). Merge order: this side first, then the agent-core scenarios (the shared
   cache name does not encode TEST_MODE).
3. **No background egress of the base image** (found by the E2E egress check of SCN-02, T1 iteration 2): the Ubuntu
   base image ships `fwupd` with `fwupd-refresh.timer`, which ran after a clone's boot and fetched firmware metadata
   over HTTPS. The slim step disables and masks the timer and its service (the VM has no firmware to update);
   the other network timers of the base image were already disabled.
