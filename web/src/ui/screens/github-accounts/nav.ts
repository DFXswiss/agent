// Navigation entry of the company GitHub accounts (ADR-078): after members, workgroups and devices.
import type { NavEntry } from '../../nav';

export const nav: NavEntry[] = [{ id: 'github', order: 63, to: '/o/:org/github', labelKey: 'identities.nav' }];
