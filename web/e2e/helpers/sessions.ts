// Session content through the device double (`scripts/hub_test_events.py`, WEB-05): device-authored session events
// sealed with the workgroup key and stored with every hub check, and subagent sessions. Used by the chat, sessions and
// terminal specs (`e2e/devices/`).
import { formatTime } from '../../src/core/encoding/time';
import { buildBatch } from '../../src/core/log/rotation';
import { verifyPlatformLog } from '../../src/core/log/platform';
import { epochKey } from '../../src/core/log/state';
import { compiledRootKey } from '../../build/manifest.ts';
import { DIRECTOR_ORIGIN } from '../fixtures/env.ts';
import type { PageUser } from './user.ts';

async function call(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${DIRECTOR_ORIGIN}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director POST ${path}: ${JSON.stringify(data)}`);
  return data;
}

export interface SessionEvent {
  readonly kind: string;
  readonly body: unknown;
  /** Header time relative to now (ms; negative: earlier). */
  readonly ts_offset_ms?: number;
}

/** Stores the events as the session's device; returns their `scope_seq`s. */
export async function sessionEvents(session: string, events: readonly SessionEvent[]): Promise<number[]> {
  const answer = (await call(`/sessions/${session}/events`, { events })) as { scope_seqs: number[] };
  return answer.scope_seqs;
}

/** A subagent session of `parent` (state `working` by default). */
export async function childSession(
  parent: string,
  options: { state?: string; model?: string | null } = {},
): Promise<string> {
  const answer = (await call(`/sessions/${parent}/children`, {
    state: options.state ?? 'working',
    model_id: options.model ?? null,
  })) as { session_id: string };
  return answer.session_id;
}

/** A `session.terminal` body with output frames `[t_ms, data]` (160 × 48 unless given). */
export function terminalChunk(frames: readonly [number, string][], size: [number, number] = [160, 48]) {
  return {
    kind: 'session.terminal',
    body: {
      offset_ms_start: frames[0]![0],
      frames: frames.map(([t, data]) => [t, 'o', data]),
      cols: size[0],
      rows: size[1],
    },
  };
}

/** Changes the user's own access to a workgroup with a `grant` entry (an admin may do so; WEB-06 has no UI yet). */
export async function grantOwnAccess(api: PageUser, org: string, wg: string, access: 'read' | 'write'): Promise<void> {
  const log = await api.log(org);
  const keys = await api.keys(org, log);
  const platform = (await api.request('GET', '/v1/platform/log')) as { entries: { entry: unknown }[] };
  const verified = await verifyPlatformLog(
    platform.entries.map((item) => item.entry),
    compiledRootKey(),
  );
  const built = await buildBatch({
    log,
    platform: verified.state,
    author: { id: api.userId, identity: api.identity },
    entries: [{ type: 'grant', subject: api.userId, wg, access }],
    keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
    events: [],
    ts: formatTime(Date.now()),
  });
  await api.request('POST', `/v1/orgs/${org}/log`, built.body);
}
