# CTR-03 – contract changes outside its own directories

ADR-027 section 3 (defect fixes, additive changes).

- **Vectors (defect):** `vectors/enroll.json` now uses the device capacity of `02-data-model.md` section 2.1
  (`vm_memory_budget_mib`, `vm_memory_used_mib`, `vm_slots_total`, `vm_slots_free`, `pool_ready`) instead of
  `{cpus, memory_mib}`; the negative vector `capacity_changed` changes `vm_slots_free`. `vectors/event.json`: the
  `prompt` body carries `attachments: []` (`03-api.md` section 10). `vectors/request.json`: the first `claude.login`
  request body is `{"mode":"manual"}` (`requests/claude.login.request.json`). Source change: `src/agent_hub/crypto/vectors.py` (CRY-01 generator, `build_enroll`, the prompt body of `build_event`, the `claude.login` bodies of `build_request`). The
  cryptographic rules and all other vectors are unchanged; implementations reproduce the regenerated files.
- **`api/INDEX.json`:** the 17 device and stream rows name their schemas; `pending_owners` is empty.
- **`README.md`:** layout rows for the CTR-03 directories (additive).
