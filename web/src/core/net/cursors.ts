// Cursors of the live channel (03-api.md section 5): per scope the highest `scope_seq` the client holds. Delivery is
// at-least-once, so a frame at or below the cursor is a duplicate; a frame above `cursor + 1` reveals a gap that is
// repaired by pulling `GET /v1/scopes/{scope_id}/events?after=<cursor>`.

/** The hub accepts at most this many `scope:seq` pairs in `?cursors=` (contracts/ws/sse/query.json). */
export const MAX_QUERY_CURSORS = 256;

export type Position = 'duplicate' | 'next' | 'gap';

export type CursorMap = Readonly<Record<string, number>>;

/** Mutable cursor table with a change listener (persistence, `persist/`). */
export class Cursors {
  readonly #seqs = new Map<string, number>();
  readonly #listeners = new Set<(cursors: CursorMap) => void>();

  constructor(initial: CursorMap = {}) {
    for (const [scope, seq] of Object.entries(initial)) this.#seqs.set(scope, seq);
  }

  /** The cursor of a scope; undefined for a scope the client has not seen. */
  get(scope: string): number | undefined {
    return this.#seqs.get(scope);
  }

  has(scope: string): boolean {
    return this.#seqs.has(scope);
  }

  /** Where a frame with `seq` falls relative to the cursor (an unseen scope counts from 0). */
  position(scope: string, seq: number): Position {
    const cursor = this.#seqs.get(scope) ?? 0;
    if (seq <= cursor) return 'duplicate';
    return seq === cursor + 1 ? 'next' : 'gap';
  }

  /** Moves the cursor forward to `seq` (never backwards); returns whether it moved. */
  advance(scope: string, seq: number): boolean {
    const cursor = this.#seqs.get(scope);
    if (cursor !== undefined && seq <= cursor) return false;
    this.#seqs.set(scope, seq);
    this.#notify();
    return true;
  }

  /** Sets a cursor, also backwards (a new scope, or a hub restored from a backup). */
  reset(scope: string, seq: number): void {
    if (this.#seqs.get(scope) === seq) return;
    this.#seqs.set(scope, seq);
    this.#notify();
  }

  #notify(): void {
    const snapshot = this.snapshot();
    for (const listener of this.#listeners) listener(snapshot);
  }

  /** Drops a scope (no longer readable). */
  forget(scope: string): void {
    if (!this.#seqs.delete(scope)) return;
    this.#notify();
  }

  scopes(): string[] {
    return [...this.#seqs.keys()].sort();
  }

  snapshot(): CursorMap {
    return Object.fromEntries([...this.#seqs.entries()].sort(([a], [b]) => (a < b ? -1 : 1)));
  }

  /** `scope:seq,…` for `GET /v1/stream?cursors=`: sorted, at most `MAX_QUERY_CURSORS` (the rest is pulled after `hello`). */
  query(): string {
    return Object.entries(this.snapshot())
      .slice(0, MAX_QUERY_CURSORS)
      .map(([scope, seq]) => `${scope}:${seq}`)
      .join(',');
  }

  onChange(listener: (cursors: CursorMap) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }
}
