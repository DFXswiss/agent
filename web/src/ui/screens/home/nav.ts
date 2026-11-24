// Navigation entry of the home screen (06-web.md section 12.1: Home first) with the badge of the tasks that wait for
// the user's decision (section 4.0, KOR-39).
import type { NavEntry } from '../../nav';
import { AttentionBadge } from './Badge';

export const nav: NavEntry[] = [{ id: 'home', order: 10, to: '/o/:org', labelKey: 'home.nav', Badge: AttentionBadge }];
