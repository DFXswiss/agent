# PFM-01 – decisions in the agent repository (final PERF acceptance)

1. **A hot-path clone occupies one VM slot, not two.** PERF-05 (30 first tasks at once on the runner device) failed one
   session with `capacity_exhausted` although the device had 60 slots and fewer than 40 VMs: the clone's reservation
   (`VmManager.reserved`, needed between the capacity check and the insert) stayed counted after its `assigning` row
   existed, and that row occupies a slot as well (`capacity.SLOT_STATES`, 05-runner.md section 3.4). With 28 parallel
   clones the device counted about 63 occupied slots. The reservation now ends right after the insert
   (`VmManager._slot` yields `release`); a restart keeps its reservation to the end, because its row is `stopped`
   (no slot) until it runs (a `stopping` row it restarts counts twice meanwhile, on the safe side). Test:
   `test_a_hot_path_clone_occupies_one_slot`.
2. **One lock from the capacity check to the claim.** With exact counting the apparent limit is the real one, so an
   old race mattered: the check awaits the store, and two clones of different workgroups (or a clone and a pool
   refill) could both take the last free slot and overbook the VM memory budget. `Pool.slot_lock` is now held from the
   check to the claim by `VmManager._slot` (including a needed LRU stop) and by the refill (`missing` and `filling`).
   Test: `test_two_clones_never_share_the_last_slot` (a barrier makes both checks run before either claims).
