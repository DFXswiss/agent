// Slice `joined`: the confirmation after joining an organization from an invitation (KOR-36): "You are now a member of
// <org>" stays visible in the header of that org until the user closes it or works in another org. The org name is
// the one the invitation package carried ('' if none).
import type { Slice, Store } from '../store';

export interface JoinedNotice {
  readonly org: string;
  readonly orgName: string;
}

export interface JoinedState {
  readonly notice: JoinedNotice | null;
}

declare module '../store' {
  interface StoreState {
    joined: JoinedState;
  }
}

export const slice: Slice<'joined'> = { name: 'joined', initial: () => ({ notice: null }) };

export function noteJoined(store: Store, notice: JoinedNotice): void {
  store.update('joined', () => ({ notice }));
}

export function dismissJoined(store: Store): void {
  store.update('joined', (state) => (state.notice === null ? state : { notice: null }));
}
