// Slice `rotation`: the toasts of the pending-rotation job (06-web.md section 11 job 4): every automatic rotation batch
// the browser sent leaves a notice (org, number of rotated scopes, devices unassigned after a decertification) until
// the user dismisses it. At most `MAX_NOTICES` are kept (oldest dropped).
import type { Slice, Store } from '../store';

export interface RotationNotice {
  readonly id: number;
  readonly org: string;
  /** Scopes rotated by the batch. */
  readonly scopes: number;
  /** Decertified platform devices unassigned by the batch (0 for a rotation owed after a member left). */
  readonly devices: number;
  readonly at: number;
}

export interface RotationState {
  readonly notices: readonly RotationNotice[];
  readonly next: number;
}

declare module '../store' {
  interface StoreState {
    rotation: RotationState;
  }
}

export const MAX_NOTICES = 3;

export const slice: Slice<'rotation'> = { name: 'rotation', initial: () => ({ notices: [], next: 1 }) };

export function noteRotation(store: Store, notice: Omit<RotationNotice, 'id'>): void {
  store.update('rotation', (state) => ({
    notices: [...state.notices, { ...notice, id: state.next }].slice(-MAX_NOTICES),
    next: state.next + 1,
  }));
}

export function dismissRotation(store: Store, id: number): void {
  store.update('rotation', (state) =>
    state.notices.some((notice) => notice.id === id)
      ? { ...state, notices: state.notices.filter((notice) => notice.id !== id) }
      : state,
  );
}
