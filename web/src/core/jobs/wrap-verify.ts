// Job 2 of 06-web.md section 11: wrap verification. Every wrap fetched for the user (`GET /v1/wraps`) that belongs to
// the org's scopes is checked (signature, W1-W2 against the verified log, decertified platform devices excluded) and
// opened with the unlocked identity; the scope keys go to the org's key ring in memory. Violations are ignored and
// counted (`wrap_unauthorized`: the distinct refused wraps of the org), never used. `cred` wraps belong to the
// credential job (WEB-10).
import { epochKey, ORG, WG } from '../log/state';
import { excludedFor } from '../log/platform';
import { slotKey, verifyWraps, type WrapRecord } from '../log/wraps';
import { refusalCode } from '../log/entries';
import type { Secret } from '../crypto/secret';
import { unwrapKey } from '../crypto/wrap';
import { HubError } from '../net/signedFetch';
import { addKeys, orgEntry, recordWraps } from '../store/slices/orgs';
import type { JobContext, JobRegistration, JobRun } from './index';

/** A wrap as `GET /v1/wraps` returns it (`stored_wrap`). */
export interface StoredWrap extends WrapRecord {
  readonly wrapped_by: string;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

function isStoredWrap(value: unknown): value is StoredWrap {
  return (
    isRecord(value) &&
    typeof value.scope === 'string' &&
    typeof value.scope_id === 'string' &&
    Number.isSafeInteger(value.epoch) &&
    typeof value.recipient_id === 'string' &&
    typeof value.wrap === 'string' &&
    typeof value.sig === 'string' &&
    typeof value.wrapped_by === 'string'
  );
}

/** The wraps addressed to the user (a malformed answer fails the run). */
export async function fetchWraps(context: JobContext): Promise<StoredWrap[]> {
  const body = await context.client.request('GET', '/v1/wraps');
  if (!isRecord(body) || !Array.isArray(body.wraps) || !body.wraps.every(isStoredWrap))
    throw new HubError('unavailable', 200);
  return body.wraps;
}

/** Writes nothing if the store was reset (sign-out) since `generation`. */
export async function verifyOrgWraps(
  context: JobContext,
  org: string,
  generation: number = context.store.generation,
): Promise<void> {
  const { store } = context;
  const { userId, identity } = store.getState().me;
  const log = orgEntry(store.getState().orgs, org).log;
  if (identity === null || userId === null || log === null) return;
  const state = log.state;
  const ours = (await fetchWraps(context)).filter(
    (wrap) =>
      wrap.recipient_id === userId &&
      ((wrap.scope === ORG && wrap.scope_id === org) || (wrap.scope === WG && state.workgroups.has(wrap.scope_id))),
  );
  const platform = store.getState().orgs.platform?.state ?? null;
  const fetched = ours.map((wrap) => ({
    wrap: {
      scope: wrap.scope,
      scope_id: wrap.scope_id,
      epoch: wrap.epoch,
      recipient_id: wrap.recipient_id,
      wrap: wrap.wrap,
      sig: wrap.sig,
    },
    wrappedBy: wrap.wrapped_by,
  }));
  const checked = await verifyWraps(fetched, log, excludedFor(platform, state));
  const known = orgEntry(store.getState().orgs, org).keys;
  const keys = new Map<string, Secret>();
  const refused = new Map<string, string>();
  for (const item of checked.ignored) {
    const wrap = item.wrap as WrapRecord;
    refused.set(
      slotKey({ scope: wrap.scope, scope_id: wrap.scope_id, epoch: wrap.epoch, recipient: wrap.recipient_id }),
      item.code,
    );
  }
  const accepted: string[] = [];
  for (const { slot, wrap } of checked.accepted) {
    const key = epochKey(slot.scope, slot.scope_id, slot.epoch);
    accepted.push(slotKey(slot));
    if (known.has(key) || keys.has(key)) continue;
    try {
      keys.set(key, await unwrapKey(identity.kem, slot, wrap.wrap));
    } catch (error) {
      refused.set(slotKey(slot), refusalCode(error));
    }
  }
  if (store.generation !== generation) return;
  recordWraps(
    store,
    org,
    refused,
    accepted.filter((slot) => !refused.has(slot)),
  );
  addKeys(store, org, keys);
  if (keys.size > 0) context.changed?.(org);
}

export const job: JobRegistration = {
  name: 'wrap-verify',
  order: 20,
  triggers: ['login', 'log', 'wraps'],
  run: (context: JobContext, run: JobRun) => verifyOrgWraps(context, run.org, run.generation),
};
