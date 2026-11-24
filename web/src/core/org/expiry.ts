// Expiries of invitations (ADR-005, 03-api.md section 12). A module of its own so that the platform invitations of
// the first load (`core/platform/invites.ts`) do not pull the org invitation flows (`invites.ts`).
export const HOUR_MS = 3_600_000;
/** Selectable expiries in hours (1 h – 30 d, ADR-005 / 03-api.md section 12). */
export const EXPIRY_HOURS = [1, 6, 24, 72, 168, 336, 720] as const;
/** Defaults: user invitations 72 h, device invitations 24 h. */
export const USER_EXPIRY_HOURS = 72;
export const DEVICE_EXPIRY_HOURS = 24;
