// Navigation entry of the community (14-community.md section 6): `/community` for every signed-in user, independent of
// the org in view, with the unread count as badge; hidden while signed out and on a hub without the community.
import type { StoreState } from '../../../core/store';
import type { NavEntry } from '../../nav';

export const communityHidden = (state: StoreState): boolean =>
  state.session.phase !== 'unlocked' || state.community.phase === 'absent';

export const communityUnread = (state: StoreState): number => state.community.unread;

export const nav: NavEntry[] = [
  {
    id: 'community',
    order: 50,
    to: '/community',
    labelKey: 'community.nav',
    hidden: communityHidden,
    badge: communityUnread,
  },
];
