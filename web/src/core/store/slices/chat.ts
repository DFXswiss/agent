// Slice `chat` (06-web.md section 5.2): per task the state of the timeline's first pull and the sealed prompt the
// composer keeps after a failure a retry can fix (sending the unchanged text again re-sends the identical event, so
// the hub answers `replayed`, 06-web.md section 12.5). The timeline itself is derived from the `events` and `tasks`
// slices (`core/chat/timeline.ts`).
import type { Slice, Store } from '../store';

export type LoadStatus = 'loading' | 'ready' | 'error';

export interface ChatLoad {
  readonly status: LoadStatus;
  readonly code: string | null;
}

export interface KeptPrompt {
  readonly text: string;
  readonly event: unknown;
}

export interface ChatState {
  readonly loads: Readonly<Record<string, ChatLoad>>;
  readonly kept: Readonly<Record<string, KeptPrompt>>;
}

declare module '../store' {
  interface StoreState {
    chat: ChatState;
  }
}

export const slice: Slice<'chat'> = { name: 'chat', initial: () => ({ loads: {}, kept: {} }) };

export function setLoad(store: Store, task: string, status: LoadStatus, code: string | null = null): void {
  store.update('chat', (state) => {
    const known = state.loads[task];
    if (known !== undefined && known.status === status && known.code === code) return state;
    return { ...state, loads: { ...state.loads, [task]: { status, code } } };
  });
}

/** Keeps (or with `null` forgets) the sealed prompt of a task. */
export function keepPrompt(store: Store, task: string, kept: KeptPrompt | null): void {
  store.update('chat', (state) => {
    if (kept === null) {
      if (state.kept[task] === undefined) return state;
      const { [task]: _sent, ...rest } = state.kept;
      return { ...state, kept: rest };
    }
    return { ...state, kept: { ...state.kept, [task]: kept } };
  });
}
