// Job 3 of 06-web.md section 11: catch-up wrapping (04-crypto.md section 5.11, 03-api.md section 4.3). After every log
// change (sign-in, SSE `log` frame) and every `INTERVAL_MS`: `GET /v1/orgs/{org}/catch-up` lists the slots of the
// current epochs (every epoch for an admin reading a workgroup by inheritance, ADR-058) without a wrap that the user may
// create; the browser creates and uploads (`POST /v1/wraps`) only those the verified log confirms (W1/W2, an epoch
// `catchUpEpochs` gives, recipient entitled now, not withheld by the transition rule `compatSkip`, recipient key from
// the verified log – never from the hub):
// - `org`/`wg` slots with the scope keys the user holds (`catchUpWraps` of `core/log`), including every epoch of a
//   workgroup released to DFX support for every person of the support roster of the verified platform log (ADR-096:
//   their keys come from that log's root certifications, never from the hub);
// - the user's own `cred` slots (devices entitled to the user's `CK`, 04-crypto.md section 5.2a): `CK` is opened from
//   the user's own self-wrap of the newest epoch (signature checked with the own identity); slots of another owner
//   or another epoch, and recipients the verified logs do not entitle (`mayWrapCred` over every verified org of the
//   user), are dropped.
// Nothing is sent when nothing is left; a run that started before a sign-out sends nothing.
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import { unwrapKey, verifyWrap } from '../crypto/wrap';
import { buildWraps, catchUpWraps } from '../log/catchup';
import { excludedFor } from '../log/platform';
import { compareSlots, CRED, mayWrapCred, slotKey, type WrapRecord, type WrapSlot } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { selfWrapperKey } from '../github/connection';
import { keyRing, verifiedPlatform } from '../org/batch';
import { orgEntry } from '../store/slices/orgs';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';

/** The periodic trigger of catch-up wrapping and pending rotations (06-web.md section 11: every 10 minutes). */
export const INTERVAL_MS = 600_000;

const isSlot = (value: unknown): value is { scope: string; scope_id: string; epoch: number; recipient_id: string } =>
  isRecord(value) &&
  typeof value.scope === 'string' &&
  typeof value.scope_id === 'string' &&
  Number.isSafeInteger(value.epoch) &&
  typeof value.recipient_id === 'string';

/** The hub's offer (`{missing: [slot]}`); a malformed answer fails the run. */
export async function fetchMissing(context: JobContext, org: string): Promise<WrapSlot[]> {
  const body = await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/catch-up`);
  if (!isRecord(body) || !Array.isArray(body.missing) || !body.missing.every(isSlot)) {
    throw new HubError('unavailable', 200);
  }
  return body.missing.map((slot) => ({
    scope: slot.scope,
    scope_id: slot.scope_id,
    epoch: slot.epoch,
    recipient: slot.recipient_id,
  }));
}

/** The user's `CK` of the newest epoch the user (or its active agent) wrapped for the user and that opens, or null. */
export async function ownCredentialKey(
  context: JobContext,
  userId: string,
  identity: Identity,
): Promise<{ epoch: number; key: Secret } | null> {
  const body = await context.client.request('GET', `/v1/wraps?scope=cred&scope_id=${encodeURIComponent(userId)}`);
  const wraps = isRecord(body) && Array.isArray(body.wraps) ? body.wraps.filter(isRecord) : [];
  const own = wraps
    .filter((w) => w.scope === CRED && w.scope_id === userId && w.recipient_id === userId)
    .filter((w) => Number.isSafeInteger(w.epoch) && typeof w.wrap === 'string')
    .sort((a, b) => (b.epoch as number) - (a.epoch as number));
  for (const found of own) {
    const slot = { scope: CRED, scope_id: userId, epoch: found.epoch as number, recipient: userId };
    try {
      const wrapper = selfWrapperKey(context.store, userId, identity, found.wrapped_by) ?? fail('author_not_writer');
      await verifyWrap(wrapper, slot, found.wrap as string, found.sig);
      return { epoch: slot.epoch, key: await unwrapKey(identity.kem, slot, found.wrap as string) };
    } catch {
      // a wrap that does not verify or open is skipped (an older one may)
    }
  }
  return null;
}

/** The `cred` wraps of the offer that the verified logs of all the user's orgs confirm. */
export async function credWraps(
  context: JobContext,
  userId: string,
  identity: Identity,
  offered: readonly WrapSlot[],
): Promise<WrapRecord[]> {
  const mine = offered.filter((slot) => slot.scope === CRED && slot.scope_id === userId);
  if (mine.length === 0) return [];
  const own = await ownCredentialKey(context, userId, identity);
  if (own === null) return [];
  const { orgs } = context.store.getState();
  const states = Object.values(orgs.byId).flatMap((entry) =>
    entry.log === null || entry.blocked !== null ? [] : [entry.log.state],
  );
  const platform = verifiedPlatform(context.store);
  const excluded = states.flatMap((state) => excludedFor(platform, state));
  const unique = new Map(mine.map((slot) => [slotKey(slot), slot]));
  const slots = [...unique.values()]
    .filter((slot) => slot.epoch === own.epoch && mayWrapCred(states, slot, userId, own.epoch, excluded))
    .sort(compareSlots);
  return buildWraps(identity, slots, () => own.key, states);
}

/** One run for `org`. */
export async function catchUp(context: JobContext, run: JobRun): Promise<number> {
  const { store } = context;
  const { userId, identity } = store.getState().me;
  const entry = orgEntry(store.getState().orgs, run.org);
  if (userId === null || identity === null || entry.log === null) return 0;
  const offered = await fetchMissing(context, run.org);
  if (offered.length === 0) return 0;
  const wraps = [
    ...(await catchUpWraps(entry.log, userId, identity, offered, keyRing(store, run.org), verifiedPlatform(store))),
    ...(await credWraps(context, userId, identity, offered)),
  ];
  if (wraps.length === 0 || !current(context, run)) return 0;
  await context.client.request('POST', '/v1/wraps', { body: { wraps } });
  return wraps.length;
}

export const job: JobRegistration = {
  name: 'catch-up',
  order: 30,
  triggers: ['login', 'log', 'interval'],
  run: async (context: JobContext, run: JobRun) => {
    await catchUp(context, run);
  },
};
