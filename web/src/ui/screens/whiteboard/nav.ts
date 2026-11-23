// Navigation entry of the whiteboards (06-web.md section 6.1): the org board, which links the workgroup boards.
import type { NavEntry } from '../../nav';

export const nav: NavEntry[] = [{ id: 'whiteboard', order: 40, to: '/o/:org/whiteboard', labelKey: 'whiteboard.nav' }];
