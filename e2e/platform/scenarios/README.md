# Model-double scenarios of the E2E specs

One file per spec that uses the model double: `e2e-NN-<slug>.json`, lower case like the spec it belongs to
(`specs/e2e-NN-<slug>.spec.ts`, ADR-027 section 4). Format: the scenario files of `agent_doubles.model`
(`steps` with `expect`/`reply`/`error`, optional `side`, `variables`, `openrouter`; see the module docstring of
`agent_doubles/model/scenario.py` in the agent repository).

How a spec uses a scenario:

```ts
const scenario = JSON.parse(readFileSync(new URL('../scenarios/e2e-06-first-task.json', import.meta.url), 'utf8'));
await orchestrator.loadScenario('model', scenario); // POST /doubles/model/scenario
// ... drive the browser ...
expect((await orchestrator.doubleState('model')).ok).toBe(true);
```

The orchestrator boots the model double with its own `orchestrator/idle-scenario.json` (no request expected). At
the end of every run it checks each double: with a spec scenario loaded, `state.ok` must be true (every expected
request arrived, no failure); with the idle scenario still loaded, no request may have arrived at all.

Deterministic rules (07-testing.md section 5.2): fixed texts, no time-dependent replies; IDs the API lets clients
choose are fixed in the spec.
