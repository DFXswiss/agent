// Job 5 of 06-web.md section 11: credential re-wrap (04-crypto.md sections 5.10 and 6.3 W2, 03-api.md section 4.8).
//
// A device is entitled to the user's credential key `CK` while it is an active device assigned to a workgroup in
// which the user has access (`credReaders` over the verified logs of all the user's orgs, decertified platform devices
// excluded).
// - **A device becomes eligible:** its wrap of the current `CK` is created by the catch-up job (job 3, `credWraps`:
//   the hub offers the missing `cred` slot, the browser checks it against the verified logs and wraps). This job adds
//   nothing there, so a slot is never wrapped twice.
// - **A device loses eligibility:** it must not read credentials stored from now on, so `CK` is rotated: a fresh
//   `CK_{e+1}`, wrapped for the user and every device entitled now, and every record of the user re-encrypted under
//   it by the user (same ID and kind, `version + 1`), atomically in `POST /v1/me/credentials/rotate`. A toast tells
//   the user (slice `credentials`).
//
// When is a rotation owed? The browser decides from what it verified, plus the hub's list of the current holders:
// - `credentialLosses` replays the verified logs: after every entry, the devices that were readers of the user's `CK`
//   before and are not after it (an unassignment, a removal, an `ungrant` of the user, the user's own removal) and
//   that are not readers now (through any org) lost eligibility at the entry's signed `ts`; a decertified platform
//   device at the signed `ts` of its newest `device_decertify`;
// - `removalLosses`: an org whose verified log the browser holds but that `GET /v1/me` no longer lists (the user was
//   removed and cannot read the `remove` entry): its devices that are readers through no other org are lost now;
// - `holderLosses` (KOR-04, AUD-01 A-07): the hub names the devices holding a wrap of the user's current `CK`
//   (`cred_holders` of `GET /v1/me`, IDs only); one that no verified log of an org the user belongs to entitles is lost
//   now – this covers an org the user left while no browser of the user held its log. The hub can only cause a
//   rotation this way, never prevent one the logs owe (the new wraps go to the readers of the verified logs only). It
//   counts once the browser has a verdict on every org `GET /v1/me` lists (verified at least up to the hub's head, or
//   blocked: a blocked org entitles nobody); while one is still being verified it waits for the next run, so a fresh
//   sign-in or a log that has not caught up does not rotate the key away from an entitled device. Holders of another
//   epoch than the newest own `CK` are ignored (another browser's rotation is in flight). A holder loss is handled
//   once per device and sign-in, whatever the epoch: when another view (a browser that does not block an org this
//   one blocks, or that verified a newer log) wraps the next `CK` for the device again, this browser does not keep
//   rotating against it. Against a hub that leaves holders out the check can only be as good as the hub (trust
//   boundary: the hub may withhold); the log-based losses above stay independent of it. Records written by such a
//   holder are deleted like those of any device that lost eligibility (the browser cannot know its keys: it never
//   held that org's log), instead of blocking the rotation.
// The new epoch follows the newest self-wrap the hub lists, also one this browser does not take (an epoch a since
// revoked agent of the user created, ADR-082 section 9.5). A rotation is owed for a loss not yet handled in this sign-in that is
// not clearly older than the start of the current epoch (the hub's `created_at` of the user's own self-wrap of the
// epoch) – "clearly" allows `SKEW_MS` between the entry signer's clock and the hub's (signed requests are bounded to
// 120 s). An unknown or future epoch start fails closed (rotate). The `handled` set prevents repeated rotations for the
// same loss: within the sign-in, and – for losses with a signed time and losses through a left org – across sign-ins
// and reloads of this browser, which keeps them in its preferences (`handledFor`, `rememberHandled`); otherwise every
// sign-in within the margin of the epoch its own rotation created would rotate again (and race a pairing being
// confirmed). Another browser may rotate once more, which only costs a re-encryption. Holder losses stay per sign-in (a
// device that holds the key again is a new case).
//
// Records: written by the user, a device entitled now or an agent of the user (also a replaced or revoked one: its
// records are re-signed by the user, ADR-082 section 9.5) → re-encrypted by the user. Written by a device that has lost
// eligibility → deleted (re-signing them as the user's would launder whatever that device or a colluding hub wrote;
// the user connects again). A rotation runs silently (ADR-058 section 4). Any other record the browser cannot open
// (signature, body, epoch) blocks the rotation – nothing is deleted on the browser's own doubt – and a notice asks the
// user to delete or replace it (shown once per sign-in).
//
// `CK` belongs to the user while jobs run per org queue: only the queue of the first verified org acts, runs are
// serialized per store (`exclusive`), and a run that finds the rotation done by another run or browser does nothing.
// A `409 version_conflict` (another browser rotated or wrote a record in between) is retried once from the start.
//
// GitHub identities (ADR-078 section 1): the rotation body holds the primary records only (`/v1/me/credentials` lists
// one record per kind, the contract allows four records); right after the rotation the user's further `github` records
// are re-sealed under the new epoch (`rewrapOwnIdentities`, same ID, version + 1; the identity module is imported only
// then, so it stays out of the first load, 06-web.md section 12.2). Should that fail, the job `github-rewrap` does it
// on its next run.
//
// The losses, the rotation and the run are in `credentials/rotation.ts`, imported when a run needs it (not part of the
// first load, 06-web.md section 12.2).
import type { VerifiedLog } from '../log/verify';
import type { Store } from '../store/store';
import type { JobContext, JobRegistration, JobRun } from './index';

/** The verified logs of the user's orgs (blocked orgs left out), ordered by org ID. */
export function verifiedLogs(store: Store): VerifiedLog[] {
  return Object.values(store.getState().orgs.byId)
    .flatMap((entry) => (entry.log === null || entry.blocked !== null ? [] : [entry.log]))
    .sort((a, b) => Number(a.state.org > b.state.org) - Number(a.state.org < b.state.org));
}

/** Runs of one store, one at a time (`CK` belongs to the user, the queues are per org). */
const running = new WeakMap<Store, Promise<unknown>>();

export function exclusive<T>(store: Store, task: () => Promise<T>): Promise<T> {
  const before = running.get(store) ?? Promise.resolve();
  const next = before.then(task);
  running.set(
    store,
    next.catch(() => undefined),
  );
  return next;
}

export const job: JobRegistration = {
  name: 'credential-rewrap',
  order: 35,
  triggers: ['login', 'log', 'interval'],
  run: async (context: JobContext, run: JobRun) => {
    // loaded when the job runs, so the rotation stays out of the first load (06-web.md section 12.2)
    await import('../credentials/rotation').then((rotation) => rotation.rewrap(context, run));
  },
};
