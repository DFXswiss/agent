// The bandwidth choice of the live picture (06-web.md section 5.4b, ADR-085 section 6): "Economical" (250 kbit/s),
// "Standard" (1000, default) and "Sharp" (3000), kept in this browser (`Prefs`, local storage) and sent as
// `browser_kbps` of the live channel.
export const BANDWIDTHS = { economical: 250, standard: 1000, sharp: 3000 } as const;
export type Bandwidth = keyof typeof BANDWIDTHS;
export const BANDWIDTH_IDS = Object.keys(BANDWIDTHS) as Bandwidth[];
export const DEFAULT_BANDWIDTH: Bandwidth = 'standard';
/** `browser_kbps` without a watcher's wish (the hub's default). */
export const DEFAULT_KBPS = BANDWIDTHS[DEFAULT_BANDWIDTH];

export const isBandwidth = (value: unknown): value is Bandwidth =>
  typeof value === 'string' && Object.hasOwn(BANDWIDTHS, value);

/** A stored choice, or the default when nothing (valid) is stored. */
export const bandwidthOf = (stored: string | null): Bandwidth => (isBandwidth(stored) ? stored : DEFAULT_BANDWIDTH);

export const kbpsOf = (bandwidth: Bandwidth): number => BANDWIDTHS[bandwidth];
