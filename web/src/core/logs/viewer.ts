// The state of an open log viewer (ADR-080 section 3, rule "lists must not break with any data size"): the viewer
// never holds the whole log as text, only one page (one chunk of at most 256 KiB, `reader.ts`). `open` reads and
// checks the manifest and shows the first page, `go` moves to another page (a late answer for a page the user left is
// dropped), `download` reads every chunk for a file. After `close` nothing is reported any more. React reads the
// state through `subscribe` / `snapshot` (`useSyncExternalStore`).
import { requestErrorCode } from '../requests/client';
import type { TasksContext } from '../tasks/pull';
import { LogPages, type LogSource, openLog, type OpenedLog, readAll } from './reader';

export interface ViewerState {
  readonly status: 'loading' | 'ready' | 'failed';
  /** Why the log or the current page could not be read. */
  readonly code: string | null;
  readonly chunks: number;
  /** The current page. */
  readonly index: number;
  /** The text of the current page, null while it loads. */
  readonly text: string | null;
  readonly truncated: boolean;
  readonly stored: number;
  readonly bytes: number;
  readonly downloading: boolean;
  readonly downloadCode: string | null;
}

const INITIAL: ViewerState = {
  status: 'loading',
  code: null,
  chunks: 0,
  index: 0,
  text: null,
  truncated: false,
  stored: 0,
  bytes: 0,
  downloading: false,
  downloadCode: null,
};

export class LogViewer {
  #state = INITIAL;
  readonly #listeners = new Set<() => void>();
  #log: OpenedLog | null = null;
  #pages: LogPages | null = null;
  #closed = false;
  #wanted = 0;

  constructor(
    readonly context: TasksContext,
    readonly source: LogSource,
  ) {}

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly snapshot = (): ViewerState => this.#state;

  #set(patch: Partial<ViewerState>): void {
    if (this.#closed) return;
    this.#state = { ...this.#state, ...patch };
    for (const listener of this.#listeners) listener();
  }

  /** Reads the manifest and the first page. */
  async open(): Promise<void> {
    this.#closed = false;
    try {
      const log = await openLog(this.context, this.source);
      this.#log = log;
      this.#pages = new LogPages(this.context, log);
      this.#set({
        status: 'ready',
        chunks: log.chunks,
        truncated: log.truncated,
        stored: log.stored,
        bytes: log.bytes,
      });
    } catch (error) {
      this.#set({ status: 'failed', code: requestErrorCode(error) });
      return;
    }
    await this.go(0);
  }

  /** Shows page `index` (kept within the log). */
  async go(index: number): Promise<void> {
    const page = Math.max(0, Math.min(index, this.#state.chunks - 1));
    this.#wanted = page;
    this.#set({ index: page, text: null, code: null });
    try {
      const text = await this.#pages!.page(page);
      if (this.#wanted === page) this.#set({ text });
    } catch (error) {
      if (this.#wanted === page) this.#set({ code: requestErrorCode(error) });
    }
  }

  /** Every chunk of the log for a file; null when it failed (the state names why) or the viewer was closed. */
  async download(): Promise<Uint8Array[] | null> {
    this.#set({ downloading: true, downloadCode: null });
    try {
      const parts = await readAll(this.context, this.#log!);
      this.#set({ downloading: false });
      return this.#closed ? null : parts;
    } catch (error) {
      this.#set({ downloading: false, downloadCode: requestErrorCode(error) });
      return null;
    }
  }

  close(): void {
    this.#closed = true;
  }
}
