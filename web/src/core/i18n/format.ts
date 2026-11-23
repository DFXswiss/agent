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

/** Absolute local event time `HH:MM:SS.mmm`, prefixed with the date when it is not today. */
export function formatEventTime(epochMs: number, { locale, timeZone, now }: TimeOptions): string {
  const time = new Intl.DateTimeFormat(locale, {
    timeZone,
    hourCycle: 'h23',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    fractionalSecondDigits: 3,
  }).format(epochMs);
  const day = new Intl.DateTimeFormat(locale, { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' });
  const date = day.format(epochMs);
  return date === day.format(now) ? time : `${date} ${time}`;
}
