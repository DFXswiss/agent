// Navigation entry of the task overview (KOR-32): directly below Home (order 10), before every other entry.
import type { NavEntry } from '../../nav';

export const nav: NavEntry[] = [{ id: 'tasks', order: 15, to: '/o/:org/tasks', labelKey: 'tasks.nav' }];
