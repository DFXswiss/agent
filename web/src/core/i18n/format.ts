// Numbers, money and times through Intl with the active locale (06-web.md sections 2.5 and 12.4).
import type { Locale } from './locale';

export function formatNumber(value: number, locale: Locale): string {
  return new Intl.NumberFormat(locale).format(value);
}

/** US dollars; four decimals below one dollar, otherwise two. */
export function formatMoney(usd: number, locale: Locale): string {
  const digits = Math.abs(usd) < 1 ? 4 : 2;
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(usd);
}

export interface TimeOptions {
  readonly locale: Locale;
  readonly timeZone: string;
  readonly now: number;
}

/** The two formats of an event time per locale and time zone (created once: the chat shows thousands of times). */
const timeFormats = new Map<string, { time: Intl.DateTimeFormat; day: Intl.DateTimeFormat }>();

function timeFormat(locale: Locale, timeZone: string): { time: Intl.DateTimeFormat; day: Intl.DateTimeFormat } {
  const key = `${locale} ${timeZone}`;
  let formats = timeFormats.get(key);
  if (formats === undefined) {
    formats = {
      time: new Intl.DateTimeFormat(locale, {
        timeZone,
        hourCycle: 'h23',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        fractionalSecondDigits: 3,
      }),
      day: new Intl.DateTimeFormat(locale, { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }),
    };
    timeFormats.set(key, formats);
  }
  return formats;
}

/** Absolute local event time `HH:MM:SS.mmm`, prefixed with the date when it is not today. */
export function formatEventTime(epochMs: number, { locale, timeZone, now }: TimeOptions): string {
  const { time, day } = timeFormat(locale, timeZone);
  const date = day.format(epochMs);
  return date === day.format(now) ? time.format(epochMs) : `${date} ${time.format(epochMs)}`;
}

let zone: string | null = null;

/** The user's time zone (06-web.md section 12.4), resolved once per page load instead of once per shown time. */
export function localTimeZone(): string {
  zone ??= Intl.DateTimeFormat().resolvedOptions().timeZone;
  return zone;
}
