// WEB-09 calls of the device double (scripts/hub_test_operations.py, scripts/hub_test_requests.py): operations the
// session's device registers and finishes through the hub's device API, and answers to device requests; plus a
// settings event written through the API (the "other editor" of the concurrent-edit spec).
import { bodyJson } from '../../src/core/encoding/json';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatTime } from '../../src/core/encoding/time';
import { newHeader, sealEvent } from '../../src/core/crypto/event';
import { epochKey, WG } from '../../src/core/log/state';
import { directorOrigin } from '../fixtures/env.ts';
import type { PageUser } from './user.ts';

async function call(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${directorOrigin()}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director POST ${path}: ${JSON.stringify(data)}`);
  return data;
}

export interface Registered {
  readonly operation_id: string;
  readonly state: string;
  readonly duplicate_of?: string;
  readonly child_session_id?: string;
}

export type Outcome =
  | { readonly state: 'succeeded'; readonly result?: unknown }
  | { readonly state: 'failed'; readonly error: { readonly code: string; readonly retryable?: boolean } };

/** An operation of `session` as its device registers it (and finishes it with `outcome`). */
export async function operation(
  session: string,
  type: string,
  params: Record<string, unknown>,
  outcome?: Outcome,
  dedupeExtra?: Record<string, unknown>,
): Promise<Registered> {
  const body = { type, params, ...(outcome && { outcome }), ...(dedupeExtra && { dedupe_extra: dedupeExtra }) };
  return (await call(`/sessions/${session}/operations`, body)) as Registered;
}

export interface Step {
  readonly body: unknown;
  readonly final?: boolean;
  /** Writes the `github` credential with this login (and source and scopes) first (what the device does before
   *  `done`). */
  readonly store?: { readonly login: string; readonly source?: 'device_flow' | 'token'; readonly scopes?: string[] };
}

/** Answers the newest open device request of `device`; returns the request and its opened body. */
export async function answerRequest(
  device: string,
  steps: readonly Step[],
): Promise<{ request_id: string; kind: string; body: unknown }> {
  return (await call('/requests/answer', { device, steps })) as { request_id: string; kind: string; body: unknown };
}

/** A `settings.workgroup` event written by `user` through the API. */
export async function writeSettings(user: PageUser, org: string, wg: string, settings: unknown): Promise<void> {
  const log = await user.log(org);
  const keys = await user.keys(org, log);
  const epoch = log.state.epoch(WG, wg);
  const header = newHeader({
    id: uuid7(),
    org,
    scope: WG,
    scope_id: wg,
    epoch,
    author: user.userId,
    kind: 'settings.workgroup',
    ts: formatTime(Date.now()),
    lh: log.head.seq,
    wg,
  });
  const event = await sealEvent(
    keys.get(epochKey(WG, wg, epoch))!,
    header,
    bodyJson(settings),
    user.identity.sig.privateKey,
  );
  await user.request('POST', '/v1/events', { events: [{ event }] });
}
