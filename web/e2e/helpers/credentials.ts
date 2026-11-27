// WEB-10 calls of the device double (scripts/hub_test_requests.py, scripts/hub_test_metrics.py): answers to device
// requests (a step may first store the credential the device produces – `github` or `claude_subscription` – as the
// device, CK-encrypted), subscription quota windows reported by the polling device, and the unassignment of a device
// by the admin (through the API, as the devices screen would send it) that makes the browser rotate its CK.
import { formatTime } from '../../src/core/encoding/time';
import { buildBatch } from '../../src/core/log/rotation';
import { epochKey } from '../../src/core/log/state';
import { directorOrigin } from '../fixtures/env.ts';
import type { PageUser } from './user.ts';

async function post(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${directorOrigin()}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director POST ${path}: ${JSON.stringify(data)}`);
  return data;
}

export interface Answer {
  readonly body: unknown;
  readonly final?: boolean;
  readonly store?: { readonly login: string } | { readonly kind: 'claude_subscription'; readonly email: string | null };
}

/** Answers the newest open device request of `device`; returns the request and the body the device opened. */
export async function answer(
  device: string,
  steps: readonly Answer[],
  /** `request_id`: answer that open request; `skip`: the newest open one not named (logins side by side, KOR-73). */
  which: { readonly request_id?: string; readonly skip?: readonly string[] } = {},
): Promise<{ request_id: string; kind: string; body: unknown }> {
  return (await post('/requests/answer', { device, steps, ...which })) as {
    request_id: string;
    kind: string;
    body: unknown;
  };
}

export interface Window {
  readonly window: string;
  readonly used_percent: number;
  readonly resets_in_s?: number | null;
  readonly severity?: string | null;
}

/** The subscription windows of `owner` as the polling device reports them. */
export async function reportQuotas(device: string, owner: string, windows: readonly Window[]): Promise<void> {
  await post(`/devices/${device}/quotas`, { owner_id: owner, windows });
}

/** The admin unassigns `device` from `wg` (with the rotations the batch owes). */
export async function unassign(api: PageUser, org: string, wg: string, device: string): Promise<void> {
  const log = await api.log(org);
  const keys = await api.keys(org, log);
  const built = await buildBatch({
    log,
    author: { id: api.userId, identity: api.identity },
    platform: await api.platform(),
    entries: [{ type: 'device_unassign', device, wg }],
    keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
    ts: formatTime(Date.now()),
  });
  await api.request('POST', `/v1/orgs/${org}/log`, built.body);
}
