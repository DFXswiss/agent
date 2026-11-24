# KOR-49: decisions of the end-to-end scenario E2E-59

Spec `e2e/platform/specs/e2e-59-no-approval.spec.ts`, model-double scenario `e2e/platform/scenarios/e2e-59-no-approval.json`,
row E2E-59 in [07-testing.md](../spec/07-testing.md) section 2.1. Design: [ADR-077](../adr/077-no-approval-as-precondition.md).

1. **One spec, the order of the model double.** The double consumes its steps strictly in order, so the spec runs the
   parts one after another: switch off, switch on, operator subscriptions, model access, branches, the forged switch
   entry, registration, credit. Parts that never reach the model (a waiting task, refusals) need no step.
2. **The operator and the founder are agents alone.** Both register with `agent-platform register --own-account` and
   an operator invitation; the founder creates the team organizations and the tasks with `agent-platform task create`,
   which is exactly the path that now assigns the platform device itself (`ensure_device`). The person with the
   own-access preference works in the browser, because the card `chat.model-access` is part of the evidence.
3. **Switches start explicitly off and end off.** The spec appends `set-policy … off` for both switches first, so the
   request rule's "answered" time is defined, and turns both off again at the end: later specs (E2E-05) expect a team
   organization without platform access.
4. **Operator subscriptions on a further runner instance.** The stack had no operator-subscription directory. The
   orchestrator's spawn route got the option `operator_subscriptions` (a 0700 directory beside the instance's home with
   one 0600 token file of the subscription-token shape, random per spawn, removed with the instance). The model double
   checks no credential in this spec (`loadScenario` switches the access check off), so the observable is the session's
   access kind at the hub: `platform` before the grant, `subscription` after `grant-operator-subscriptions`, `platform`
   after `--revoke`. "Without a restart": between the operator command and the next task the spec only waits until the
   runner stored the hub's platform-log head (`runner.platform_log_entry`); nothing restarts the instance. The team
   organization gets the instance by `device assign --org … --device …`, so the scheduler has exactly one candidate.
5. **The forged switch entry is sent by the test itself.** The operator CLI signs with the root key and refuses a
   foreign key locally (`platform_root_mismatch`) before the hub sees anything. The spec therefore builds a
   `platform_policy` entry signed by a random Ed25519 key and sends it in a root-signed request (the test root key is
   public, WEB-01 decision 12): the hub must refuse the entry itself (`400 log_signature`); head, switch (`settings`)
   and the runner's verified copy (head equal, `platform_stopped` absent) stay unchanged.
6. **Billing is switched on only when no price list is in force** (a full run has it from E2E-46), with the start
   credit of E2E-46 (5 USD, a hub restart), so organizations of later specs or of a retry are not exhausted at once;
   the team organization's credit is then set to 0. Billing stays on afterwards, as after E2E-46.
7. **The push to `main` uses the device-flow connection of E2E-22** and no repository list (KOR-30: every repository
   the connection reaches). The protected list is saved in the workgroup settings form; the refused commit names no
   checkout, because the protected-branch check comes before the GitHub and workspace checks.
