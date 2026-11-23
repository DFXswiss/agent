// Slice `community` (12-community.md section 6): the state of the community for the signed-in user – whether the hub
// has one (`absent` hides the area), the key state (waiting for a key since when), mute and moderator flags, the read
// position and unread count, and the loaded messages ascending by `seq` with their checked plaintext (name and text of
// a verified message only; an unverifiable one carries no content, a removed one none either). The community job
// (`core/community/engine.ts`) writes it; the live frame `community` reaches the engine through `onCommunityFrame`, and
// a `delete` drops the plaintext of that `seq` here at once, before anything else runs.
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

export type CommunityPhase = 'idle' | 'loading' | 'ready' | 'absent' | 'error';
/** `none` before the key flow ran, `waiting` for a key from another user, `ready` with the key, `failed` otherwise. */
export type KeyState = 'none' | 'waiting' | 'ready' | 'failed';

export interface CommunityMessage {
  readonly seq: number;
  readonly id: string;
  readonly author: string;
  /** The hub's receive time (Unix ms). */
  readonly at: number;
  readonly removed: boolean;
  /** `pending`: not opened yet (no key); `unverifiable`: a check failed, no content is shown. */
  readonly status: 'pending' | 'verified' | 'unverifiable';
  readonly name: string | null;
  readonly text: string | null;
  readonly moderator: boolean;
}

export interface CommunityState {
  readonly phase: CommunityPhase;
  readonly code: string | null;
  readonly key: KeyState;
  readonly keyCode: string | null;
  /** When this browser started to wait for the key (Unix ms), null while not waiting. */
  readonly waitingSince: number | null;
  readonly me: string | null;
  readonly muted: boolean;
  readonly posted: boolean;
  readonly moderator: boolean;
  readonly lastRead: number;
  /**
   * Unread messages the hub counted in `(baseRead, baseHead]` (its read position and newest `seq` at the last
   * `GET /v1/community`); later messages and read progress since are counted from the loaded ones.
   */
  readonly unreadBase: number;
  readonly baseRead: number;
  readonly baseHead: number;
  readonly unread: number;
  readonly messages: readonly CommunityMessage[];
  /** Older messages exist at the hub (`next_before` of the oldest page). */
  readonly older: boolean;
  /** The newest page was loaded (live pulls continue from it). */
  readonly loaded: boolean;
  readonly textChars: number;
  readonly nameChars: number;
  /** After a refused post (`429`): when the next post may be sent (Unix ms, from `Retry-After`), null otherwise. */
  readonly retryAt: number | null;
}

declare module '../store' {
  interface StoreState {
    community: CommunityState;
  }
}

export const initialCommunity = (): CommunityState => ({
  phase: 'idle',
  code: null,
  key: 'none',
  keyCode: null,
  waitingSince: null,
  me: null,
  muted: false,
  posted: false,
  moderator: false,
  lastRead: 0,
  unreadBase: 0,
  baseRead: 0,
  baseHead: 0,
  unread: 0,
  messages: [],
  older: false,
  loaded: false,
  textChars: 2000,
  nameChars: 64,
  retryAt: null,
});

export const slice: Slice<'community'> = { name: 'community', initial: initialCommunity };

/**
 * The unread count: the hub's count, less the loaded messages of others it counted that were read since, plus the
 * loaded messages of others after its newest `seq` and after the read position (removed messages never count).
 */
export function countUnread(state: CommunityState): number {
  const others = state.messages.filter((m) => m.author !== state.me && !m.removed);
  const later = others.filter((m) => m.seq > Math.max(state.baseHead, state.lastRead)).length;
  if (state.lastRead >= state.baseHead) return later;
  const read = others.filter((m) => m.seq > state.baseRead && m.seq <= state.lastRead).length;
  return Math.max(0, state.unreadBase - read) + later;
}

function recount(state: CommunityState): CommunityState {
  const unread = countUnread(state);
  return unread === state.unread ? state : { ...state, unread };
}

/** Replaces fields of the slice and recounts the unread messages. */
export function patchCommunity(store: Store, patch: Partial<CommunityState>): void {
  store.update('community', (state) => recount({ ...state, ...patch }));
}

/** Adds or replaces messages by `seq` (ascending order kept); a removed message never gets its content back. */
export function putMessages(store: Store, messages: readonly CommunityMessage[], patch: Partial<CommunityState> = {}) {
  store.update('community', (state) => {
    const bySeq = new Map(state.messages.map((m) => [m.seq, m]));
    for (const message of messages) if (bySeq.get(message.seq)?.removed !== true) bySeq.set(message.seq, message);
    const sorted = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
    return recount({ ...state, ...patch, messages: sorted });
  });
}

const REMOVED = { removed: true, status: 'verified', name: null, text: null } as const;

/**
 * Drops the plaintext of a removed message (by `seq` or by message ID); unknown ones are ignored. A removed message of
 * another user that the hub had counted as unread leaves the hub's count.
 */
export function removeMessage(store: Store, match: { readonly seq?: number; readonly id?: string }): void {
  store.update('community', (state) => {
    const hit = (m: CommunityMessage) => !m.removed && (m.seq === match.seq || m.id === match.id);
    const gone = state.messages.filter(hit);
    if (gone.length === 0) return state;
    const counted = gone.filter((m) => m.author !== state.me && m.seq > state.baseRead && m.seq <= state.baseHead);
    return recount({
      ...state,
      unreadBase: Math.max(0, state.unreadBase - counted.length),
      messages: state.messages.map((m) => (hit(m) ? { ...m, ...REMOVED } : m)),
    });
  });
}

type FrameListener = (data: { readonly change: string; readonly seq: number }) => void;

const listeners = new WeakMap<Store, FrameListener>();

/** Registers the engine of `store` for `community`, `hello` and `resync` frames (one per store; the newest wins). */
export function onCommunityFrame(store: Store, listener: FrameListener): void {
  listeners.set(store, listener);
}

/** The `community {change, seq}` data of a frame, null if malformed. */
export function frameData(data: unknown): { change: string; seq: number } | null {
  if (!isRecord(data) || typeof data.change !== 'string' || !Number.isSafeInteger(data.seq)) return null;
  return { change: data.change, seq: data.seq as number };
}

export const frames = {
  community: (store: Store, data: unknown) => {
    const frame = frameData(data);
    if (frame === null) return;
    if (frame.change === 'delete') removeMessage(store, { seq: frame.seq });
    listeners.get(store)?.(frame);
  },
  // after a reconnect or a resync: frames may have been lost
  hello: (store: Store) => listeners.get(store)?.({ change: 'hello', seq: 0 }),
  resync: (store: Store) => listeners.get(store)?.({ change: 'hello', seq: 0 }),
};
