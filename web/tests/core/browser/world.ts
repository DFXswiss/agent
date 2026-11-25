// Test helpers of the platform browser core: `browser.action` / `browser.state` bodies, read `events` entries, a hub
// double answering the record pages of the events route, and a reader that "verifies" by header.
import { vi } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import type { StoredEvent } from '../../../src/core/net/sse';
import { HubError } from '../../../src/core/net/signedFetch';
import type { RecordContext } from '../../../src/core/browser/record';
import type { EventEntry } from '../../../src/core/store/slices/events';

export const ORG = '01930000-0000-7000-8000-0000000000aa';
export const WG = '01930000-0000-7000-8000-0000000000bb';
export const TASK = '01930000-0000-7000-8000-0000000000dd';
export const SESSION = '01930000-0000-7000-8000-0000000000c1';
export const OTHER = '01930000-0000-7000-8000-0000000000c2';
export const BROWSER = 'br-0123456789ab';
export const BROWSER2 = 'br-ba9876543210';
export const TARGET = { org: ORG, wg: WG, session: SESSION };

/** A small "JPEG" whose bytes name the action. */
export const jpegOf = (seq: number): Uint8Array => new Uint8Array([0xff, 0xd8, seq & 0xff, (seq >> 8) & 0xff]);

export function action(seq: number, overrides: Record<string, unknown> = {}) {
  return {
    browser_id: BROWSER,
    seq,
    tool: 'browser_click',
    action: 'click',
    target: 'e12',
    text: null,
    url: `https://example.com/${seq}`,
    title: `Page ${seq}`,
    tab: 0,
    ok: true,
    error: null,
    notice: null,
    at: '2026-11-25T12:00:00.000Z',
    duration_ms: 120,
    image: { type: 'image/jpeg', width: 960, height: 600, data: b64uEncode(jpegOf(seq)) },
    image_omitted: null,
    ...overrides,
  };
}

export function state(overrides: Record<string, unknown> = {}) {
  return {
    browser_id: BROWSER,
    operation_id: '0190f000-0000-7000-8000-00000000c009',
    implicit: false,
    state: 'open',
    workvm_id: 'wvm-0123abcd',
    reason: null,
    url: 'https://example.com/',
    actions: 0,
    ready_ms: 4000,
    idle_seconds: 600,
    opened_at: '2026-11-25T12:00:00.000Z',
    closed_at: null,
    ...overrides,
  };
}

/** A read `events` entry at `seq`. */
export function read(
  seq: number,
  options: { kind?: string; session?: string; task?: string; status?: EventEntry['status']; body?: unknown } = {},
): EventEntry {
  const status = options.status ?? 'verified';
  const header = {
    id: `id-${seq}`,
    kind: options.kind ?? 'browser.action',
    session: options.session ?? SESSION,
    task: options.task ?? TASK,
    ts: '2026-11-25T12:00:00.000Z',
  };
  return {
    org: ORG,
    scopeId: WG,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status,
    code: status === 'unverifiable' ? 'signature_invalid' : null,
    header: status === 'verified' ? header : null,
    body: status === 'verified' ? (options.body ?? action(seq)) : null,
  };
}

/** A stored event as the hub sends it; its event carries the read entry the fake reader returns. */
export const stored = (entry: EventEntry): StoredEvent => ({
  scope_seq: entry.scopeSeq,
  received_at: null,
  event: { header: entry.header ?? { session: SESSION }, entry },
});

/**
 * A hub with the scope's matching events (ascending): it answers `GET …/events?after&limit` like the events route,
 * counts the requests and can be made to fail; `read` returns the entry each stored event carries.
 */
export class RecordHub {
  events: EventEntry[] = [];
  head = 0;
  requests: string[] = [];
  fail: HubError | null = null;
  malformed = false;

  constructor(events: EventEntry[] = []) {
    this.put(events);
  }

  put(events: EventEntry[]): void {
    this.events.push(...events);
    this.events.sort((a, b) => a.scopeSeq - b.scopeSeq);
    this.head = Math.max(this.head, ...this.events.map((item) => item.scopeSeq));
  }

  readonly request = vi.fn(async (_method: string, target: string) => {
    this.requests.push(target);
    if (this.fail !== null) throw this.fail;
    if (this.malformed) return { events: 'nope' };
    const params = new URL(target, 'https://hub.example').searchParams;
    const after = Number(params.get('after'));
    const limit = Number(params.get('limit'));
    const matching = this.events.filter((item) => item.scopeSeq > after);
    const page = matching.slice(0, limit);
    return {
      events: page.map(stored),
      next_after: matching.length > limit ? page.at(-1)!.scopeSeq : null,
      head: this.head,
    };
  });

  readonly read = vi.fn(async (_org: string, _scope: string, items: readonly StoredEvent[]) =>
    items.map((item) => (item.event as { entry: EventEntry }).entry),
  );

  context(timers?: RecordContext['timers']): RecordContext {
    return {
      client: { request: this.request as never },
      read: this.read,
      ...(timers !== undefined && { timers }),
    };
  }
}

/** Lets pending promise chains run. */
export async function settle(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setImmediate(resolve));
}
