# HOT-25 decisions

A power cut at the site restarted every device. The hub came back cleanly; the runner of the platform device needed
three repairs by hand: a fatal `origin_seq_conflict`, the volume of the work VM slots under the wrong mount point (the
work VM task failed in a loop), and a main session left `working` in a stopped VM, which never got its next prompt.
Each is closed as a class (rule [E-19](../KONZEPT.md#e-19)); nothing beyond that is added ([E-25](../KONZEPT.md#e-25)).

1. **The store's commits are durable on macOS.** `fsync()` on macOS does not empty the drive's write cache; only
   `F_FULLFSYNC` does (fsync(2)). PostgreSQL's default `wal_sync_method` there is `open_datasync`, and its manual
   ("WAL Reliability") names `fsync_writethrough` for macOS. `store.cluster.start_store` checks the value at every
   start and sets it with `ALTER SYSTEM` and one restart of the cluster when it differs, so the setup and every
   existing installation (an update restarts the daemon) run with it; a value that does not hold is
   `store_not_durable` and the daemon does not start. Only the device's own tools (`device_tools()`, the default of
   `Host`) do this: test tools that hand out a shared server change nothing there.
   - Measured on a runner device (Apple silicon, internal SSD), 600-byte inserts, one commit each: one writer
     0.10 ms → 3.8–4.8 ms per commit (≈ 9 600 → 210–260 commits/s); four writers ≈ 23 500 → ≈ 510 commits/s; eight
     writers ≈ 16 500 → ≈ 1 100 commits/s (group commit). The runner commits per state change and per outbox item,
     a few per second and session, far below that.
   - Rejected: `synchronous_commit = off` or a battery assumption (both lose acknowledged commits by design); a
     setting in `postgresql.conf` of the legacy `agent_cli.pg` (legacy code stays untouched).

2. **A conflict left by a power cut repairs itself; a real one stays fatal.** The store lost its newest commits, the
   hub already held them, and the store numbered new items the same; the first push after the start meets `409
   origin_seq_conflict`. The runner takes the hub's position when all of this holds: no push of this daemon run was
   answered yet and none was repaired (a lost tail shows up at the very first push; a second device with the same
   identity shows up later, or again after a repair), the first item after `pushed_origin_seq` is
   `pushed_origin_seq + 1` (no hole), and `GET /v1/device/state` (`active`) puts the hub at or past that item and at
   most `LOSS_LIMIT` = 500 items (one batch) ahead of `pushed_origin_seq` (a power cut takes the last moments; a
   store from an old backup is hours behind). Then `outbox.adopt_after_loss` moves every unacknowledged item by the
   difference (highest first, bodies sealed again for the new storage context, sealed events kept like `realign`),
   sets `pushed_origin_seq` to the hub's position and appends the audit `runner.alert`
   `{alert: "outbox_conflict_recovered", value: <lost items>}` (target `outbox`/`origin_seq_conflict`); the log says
   `runner.alert outbox_conflict_recovered pushed=P hub=L lost=N`. An unconfirmed position is retried. Every other
   conflict is fatal as before, with `runner.sync conflict_fatal reason=after_answer|not_contiguous|hub_behind|
   too_far`.
   - Hub contract: unchanged. `runner.alert` takes any `alert` code of the pattern `^[a-z][a-z0-9_]*$` with an
     integer value (`contracts/audit/runner.alert.json`, `agent_hub.outbox.bodies.RunnerAlert`), so the new code
     needs no hub release and no guest version (no guest contract is touched).
   - Accepted risk: a store restored from a backup taken seconds before, whose device then lost nothing else, looks
     like a lost tail and is repaired the same way – which is also the right outcome for it.
   - What the lost commits recorded locally (a session state, a delivery mark) comes back with the next reconcile
     against the hub's desired state; the hub's copy of the lost items stays authoritative.

3. **The volume of the slot homes hangs at its place after every restart.** The custom mount point of `diskutil apfs
   addVolume` does not persist (observed: the volume came back under `/Volumes/AgentWorkVM`). `workvm install` writes
   `/etc/fstab` `UUID=<volume UUID> /var/agent-workvm apfs rw` (replacing other lines for that UUID or mount point),
   and mounts a volume that hangs elsewhere back (`diskutil unmount`, `diskutil mount -mountPoint`) instead of adding
   a second one (the old `install` would have, since it only looked at the mount point). The volume is found by name
   in `diskutil list -plist`: diskutil(8) does not accept a volume name as a device.
   - Existing installations: running `workvm install` again is the update step (`work-vms.md` §3); `doctor` warns
     `workvm_volume fstab_missing` until it ran. Package installations (ADR-074) configure no work VMs, so their
     maintenance daemon is not changed.
   - The daemon runs unprivileged and cannot mount or write `/etc/fstab` (no new sudo right, E-25). It checks the
     mount point, logs `runner.workvm volume_missing` at the start, offers no work VM while it is wrong
     (`WorkVms.available`), and `doctor` fails `workvm_volume not_mounted`. `workvm status` prints where it hangs.
   - `uninstall` removes the entry with the volume and reports one that stays (`fstab`).

4. **Deleting a work VM is idempotent.** A slot without its home has no Lima home and so no instance:
   `SlotCommand.sync_runner` answers `list` with no instance and `delete` like `limactl` for a missing instance
   (`Lima.delete` returns False, the row becomes `deleted`), without running the wrapper. The reconciliation and the
   sweep also catch a failing `limactl list` per slot (`list_failed slot=<n>`) and a failing orphan deletion in the
   reconciliation (`orphan_delete_failed`), so one broken slot never ends the work VM task. An instance on the
   volume while it hung elsewhere is found again as an orphan once the volume is back and deleted by the sweep.

5. **No main session stays `working` in a dead VM.** The VM reconciliation marks a VM whose instance does not run as
   `stopped`; a main session still `agent_starting`, `prompt_delivering`, `working` or `idle` there has no agent
   process (the earlier start states are failed `device_shutdown` by the reconcile, as before). `SessionService.vms_stopped`
   (task `guestlink` after `vms_lost`, and at the end of every reconcile) moves it to `suspended` with the reason
   `device_restart` (a free reason code of `session_transition`, no contract change). The deliverer's next round
   resumes it like any suspended session: the session's own VM is started again and the agent continues with
   `--resume`, or another VM restores the newest checkpoint; a prompt that already waits is then delivered. Sessions
   with a running start or stop task, child sessions (the subagent area owns them) and sessions in a deleted VM
   (`vms_lost`, ADR-087 section 3.7) are left alone. A session without a Claude session ID yet is resumed without
   `--resume` (`prompts.resume`), like any suspended one.

6. **Tests.** Real PostgreSQL throughout: the cluster durability with a real `initdb` cluster (set once, checked at
   later starts, refused when it does not hold); the unclean shutdown (items pushed, the store loses the last ones
   and their acknowledgement, new items take their numbers, a new push loop repairs and the hub keeps its items) and
   every fatal reason; the fstab, mount point and idempotent deletion with the recording `diskutil`/`limactl`
   doubles and the real `SlotCommand` on a missing home; the restart with a working session whose waiting prompt is
   then delivered with `--resume`.

Not checked on hardware (no power cut in a test): that macOS mounts the volume by the fstab entry at boot (fstab(5)
documents it; the operator's manual repair used exactly that entry) and the durability gain of `F_FULLFSYNC` itself.
