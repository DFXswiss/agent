# KOR-43 – decisions

Test runs as platform operations (ADR-078, 13-work-vm.md §12). Small decisions of the package; the architecture is in
the ADR.

1. **Command, no templates.** `test.run` takes the command the session writes (`kind` only labels it). Fixed
   templates per language would be a restriction the session has to work around ([E-11](../KONZEPT.md#e-11)).
2. **`cwd` anywhere.** Any absolute path is allowed (the run VM is the session's own; [E-08](../KONZEPT.md#e-08)).
3. **Duplicate rule at the runner.** The hub cannot know the workspace state, so `test.run` has no hub dedupe fields
   (`dedupe: false`); the runner joins a running run of the same session with the same parameters and workspace
   digest. A finished run is never joined (asking again is asking for a new run).
4. **Operation failure vs. run error.** Until the command started, every failure fails the operation (nothing ran;
   the pipeline may retry). Afterwards the operation always succeeds and the run carries `error` with a reason – the
   pipeline never re-runs a started command.
5. **Workspace mirror instead of a work VM image** (ADR-078 §2): reproducible, no stop of the session's work VM, no
   multi-GiB copy. The harness tells the session to `workvm_sync from_vm` first when it changed files in the work VM.
   No dependency cache yet ([E-25](../KONZEPT.md#e-25)); the start time is logged per run (`runner.testrun ready
   ready_ms=`).
6. **Logs under their own labels** (design review, finding H1): a log shares the store and upload path of the context
   checkpoints but not their key, AAD or signature labels, and its manifest names `kind` and `ref`. Without this a hub
   could offer run output (which repository code controls) as a checkpoint to a restore.
7. **Logs uncompressed**, 256 KiB per chunk, so a viewer reads any window without the chunks before it.
8. **Kept part of a long output**: the last `LOG_MAX_MIB` (default 256 MiB): a failing test's summary is at the end.
9. **Separate log quota at the hub** (`AGENT_HUB_LOG_ORG_BYTES`, 100 GiB; `AGENT_HUB_LOG_KEEP`, 500 per task), so logs
   never crowd out the checkpoints a resume needs.
10. **A failed Lima deletion keeps the row `deleting`** (design review, finding H2): the slot and its pinned SSH port are
    not given to a new VM while the old one may run; the sweep (every minute) deletes it again and removes own
    instances without a row. Before, the row became `deleted` and the instance leaked until a restart.
11. **Pause rule only for idle VMs**: an idle suspension stops the VM only when no job runs in it (a long build the
    session waits for keeps running; design review M5). The credit stop is immediate.
12. **Run VMs and limits**: counted against the organization and owner VM limits (they bound one organization while the
    RAM share is not built), not against the per-task limit.
13. **Task VM billing** follows the workgroup VM rule: split between the task's holding sessions, the requester when
    none holds it.
14. **No release or stop button for a single run** (as for work VMs, ADR-059 UI-1): operations are requested by the
    session's device only; a person stops the session or closes the task, which cancels its runs.
15. **Log signature in the browser**: see the web reader (`web/src/core/logs/`); the AEAD of the workgroup key and the
    `ref`/object check against the encrypted event bind a log to its run.
16. **Restart capture**: after a restart a run ends `error` without its log (the VM went with the reconciliation); the
    stored tail of the `running` event is empty. Capturing before the reconciliation is left out (rare, E-25).
