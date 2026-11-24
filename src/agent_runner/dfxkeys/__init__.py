"""The DFX key issuer of a platform device (KOR-50, ADR-081 section 7, idea 46 case 3).

A platform device whose runner has `AGENT_RUNNER_OPENROUTER_MANAGEMENT_KEY_FILE` creates one limited OpenRouter key
per active org device, seals it to that device, keeps every key's limit at its spend plus the hub's headroom, reports
the provider's cumulative spend and switches keys off or deletes them when the hub says so.

- `openrouter`: the client of the OpenRouter management API (`/v1/keys`).
- `issuer`: the issuer itself (`dfx_key_state`, `dfx_keys.requests`, `dfx_keys.keys`).
- `reconcile`: step `dfx_keys` (the issuer reads the device state; an org device verifies and stores its envelopes,
  `agent_runner.model.dfxkey`).
- `tasks`: task `dfx_keys` (the issuer's work with the provider).
"""
