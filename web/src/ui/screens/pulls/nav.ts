// Navigation entry of the "Pull requests" view (KOR-48): after Tasks; on a phone under "More".
import type { NavEntry } from '../../nav';

export const nav: NavEntry[] = [{ id: 'pulls', order: 20, to: '/o/:org/pulls', labelKey: 'pullnav.nav' }];
