// Job "agents" (ADR-082 sections 4 and 9; 06-web.md section 11): every client of the user keeps the user's agents in
// the membership logs as the verified logs and the hub's agent list say. Per org, at most one batch per run:
// 1. the user's agent that is active in this log but revoked – superseded or removed otherwise in any verified log of
//    the user (`revokedAgents`), or revoked or expired at the hub (`GET /v1/me/agents`; the hub can only make a client
//    remove an agent, never keep one) – is removed: `remove{subject: agent}` by its user, with the rotations of R and
//    the wraps of the new epochs for every remaining reader (`buildBatch`). The credential-key rotation follows in the
//    job `credential-rewrap` (it runs after this one: the verified logs now owe it);
// 2. otherwise the user's own-identity agent the hub lists as active is added where it is missing (an org the user
//    joined later, a log where an earlier confirmation could not add it): `agent_add` by the user with the same
//    generation and certificate as an `agent_add`/`agent` member of a verified log of the user (the certificate is the
//    user's signature, checked there), else with the keys and certificate of the hub's list when this browser confirmed
//    that pairing with that certificate (`Prefs.confirmedAgents`) and it verifies under the user's own signing key (a
//    hub can neither make the browser add a key the user did not certify nor replay an older certificate),
//    and the wraps of every epoch the browser holds of every scope the user reads. An agent without a certificate
//    from either, one this log ever removed other than with its user, or one whose generation is not above the active
//    agent's is left alone.
// Pending rotations stay with the job `rotation`. The batch goes through `submitBatch` (rebuilt after `seq_conflict`);
// a run that started before a sign-out sends nothing.
// The work is in `agents/sync.ts`, imported when a run needs it (not part of the first load, 06-web.md section 12.2).
import type { JobContext, JobRegistration, JobRun } from './index';

export const job: JobRegistration = {
  name: 'agents',
  order: 33,
  triggers: ['login', 'log', 'interval'],
  retry: true,
  run: async (context: JobContext, run: JobRun) => {
    // loaded when the job runs, so the agent code stays out of the first load (06-web.md section 12.2)
    await import('../agents/sync').then((sync) => sync.syncAgents(context, run));
  },
};
