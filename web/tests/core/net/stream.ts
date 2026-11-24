// Test doubles of the live channel: manual timers, scripted stream connections and a pull router.
import { utf8 } from '../../../src/core/encoding/bytes';
import type { Timers } from '../../../src/core/net/sse';
import { HubError } from '../../../src/core/net/signedFetch';

export class ManualTimers implements Timers {
  now = 0;
  #next = 1;
  readonly #items = new Map<number, { at: number; callback: () => void }>();

  setTimeout(callback: () => void, ms: number): unknown {
    const id = this.#next++;
    this.#items.set(id, { at: this.now + ms, callback });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.#items.delete(handle as number);
  }

  get pending(): number[] {
    return [...this.#items.values()].map((item) => item.at - this.now).sort((a, b) => a - b);
  }

  /** Moves the clock and runs every due callback. */
  advance(ms: number): void {
    this.now += ms;
    for (const [id, item] of [...this.#items].sort(([, a], [, b]) => a.at - b.at)) {
      if (item.at > this.now) continue;
      this.#items.delete(id);
      item.callback();
    }
  }
}

/** Lets pending promise chains (and fake-indexeddb callbacks) run. */
export async function settle(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

/** One scripted stream connection: `send` text or bytes, `end` or `fail` it. */
export class Connection {
  controller!: ReadableStreamDefaultController<Uint8Array>;
  readonly target: string;
  readonly signal: AbortSignal;
  readonly response: Response;

  constructor(target: string, signal: AbortSignal) {
    this.target = target;
    this.signal = signal;
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
    });
    signal.addEventListener('abort', () => {
      try {
        this.controller.error(new DOMException('aborted', 'AbortError'));
      } catch {
        // already closed
      }
    });
    this.response = new Response(stream, { status: 200 });
  }

  send(chunk: string | Uint8Array): void {
    this.controller.enqueue(typeof chunk === 'string' ? utf8(chunk) : chunk);
  }

  frame(event: string, data: unknown): void {
    this.send(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  }

  end(): void {
    this.controller.close();
  }
}

export type PullHandler = (scope: string, after: number, limit: number) => unknown;

/** A client double: `stream` opens scripted connections (or fails with `refuse`), `request` routes pulls. */
export class FakeLiveClient {
  readonly connections: Connection[] = [];
  readonly pulls: string[] = [];
  refuse: HubError | null = null;
  pull: PullHandler = (_scope, after) => ({ events: [], next_after: null, head: after });
  #waiting: ((connection: Connection) => void)[] = [];

  stream = async (target: string, signal: AbortSignal): Promise<Response> => {
    if (this.refuse !== null) {
      const error = this.refuse;
      this.refuse = null;
      throw error;
    }
    const connection = new Connection(target, signal);
    this.connections.push(connection);
    for (const resolve of this.#waiting.splice(0)) resolve(connection);
    return connection.response;
  };

  request = async (method: string, target: string): Promise<unknown> => {
    const match = /^\/v1\/scopes\/([^/]+)\/events\?after=(\d+)&limit=(\d+)$/.exec(target);
    if (method !== 'GET' || match === null) throw new Error(`unexpected ${method} ${target}`);
    this.pulls.push(`${decodeURIComponent(match[1]!)}:${match[2]}`);
    return this.pull(decodeURIComponent(match[1]!), Number(match[2]), Number(match[3]));
  };

  get last(): Connection {
    return this.connections[this.connections.length - 1]!;
  }
}

/** A stored event with a recognizable payload. */
export const stored = (seq: number, tag = `e${seq}`) => ({
  scope_seq: seq,
  received_at: '2026-11-21T12:00:00.000Z',
  event: { header: { tag }, ct: '', sig: '' },
});
