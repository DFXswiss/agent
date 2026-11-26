// Navigation entry of the support area (ADR-096 section 4): only for a person of the support roster of the browser's
// verified platform log, independent of the org in view.
import { isSupportPerson } from '../../../core/support/names';
import type { StoreState } from '../../../core/store';
import type { NavEntry } from '../../nav';

export const supportHidden = (state: StoreState): boolean => !isSupportPerson(state);

export const nav: NavEntry[] = [
  { id: 'support', order: 80, to: '/support', labelKey: 'supportnav.nav', hidden: supportHidden },
];
