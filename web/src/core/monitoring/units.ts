// Values with units for the monitoring screen (06-web.md section 9.1, 08-monitoring.md section 6): bytes in binary
// units (KiB, MiB, …), rates per second, CPU in cores (`cpu_ratio` 1.0 = one core, 02-data-model.md section 2.8),
// shares as percent, durations in the two largest units. Numbers follow the active locale (Intl); unit symbols are the
// same in every locale, words (e.g. "cores") come from the message catalog.
import type { Locale } from '../i18n/locale';

const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'] as const;

function fixed(value: number, digits: number, locale: Locale): string {
  return new Intl.NumberFormat(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

/** `1.5 GiB`: one decimal below ten units, none from ten on and for plain bytes. */
export function formatBytes(bytes: number, locale: Locale): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${fixed(value, unit > 0 && value < 10 ? 1 : 0, locale)} ${BYTE_UNITS[unit]}`;
}

/** `1.5 MiB/s`. */
export function formatRate(bytesPerSecond: number, locale: Locale): string {
  return `${formatBytes(bytesPerSecond, locale)}/s`;
}

/** CPU in cores with two decimals (`0.25`). */
export function formatCores(ratio: number, locale: Locale): string {
  return fixed(ratio, 2, locale);
}

/** A share 0…1 as percent with at most one decimal. */
export function formatPercent(share: number, locale: Locale): string {
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 }).format(share);
}

const DURATION_UNITS: readonly [string, number][] = [
  ['d', 86_400],
  ['h', 3600],
  ['min', 60],
  ['s', 1],
];

/** `3 d 4 h`, `2 h 5 min`, `45 s`: the two largest non-zero units of whole seconds. */
export function formatDuration(seconds: number): string {
  let rest = Math.max(0, Math.floor(seconds));
  const parts: string[] = [];
  for (const [symbol, size] of DURATION_UNITS) {
    const amount = Math.floor(rest / size);
    rest -= amount * size;
    if (amount > 0 && parts.length < 2) parts.push(`${amount} ${symbol}`);
    else if (parts.length > 0) break;
  }
  return parts.length === 0 ? '0 s' : parts.join(' ');
}

/** `used / limit`, null without a positive limit. */
export function share(used: number | null, limit: number | null): number | null {
  if (used === null || limit === null || limit <= 0) return null;
  return used / limit;
}
