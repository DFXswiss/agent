// Numbers, money and times through Intl with the active locale (06-web.md sections 2.5 and 12.4).
import type { Locale } from './locale';

/** Number formats per locale and kind, created once (ADR-086 section 1: lists format thousands of numbers). */
const numberFormats = new Map<string, Intl.NumberFormat>();

function numberFormat(key: string, create: () => Intl.NumberFormat): Intl.NumberFormat {
  let format = numberFormats.get(key);
  if (format === undefined) {
    format = create();
    numberFormats.set(key, format);
  }
  return format;
}

export function formatNumber(value: number, locale: Locale): string {
  return numberFormat(locale, () => new Intl.NumberFormat(locale)).format(value);
}

/** US dollars; four decimals below one dollar, otherwise two. */
export function formatMoney(usd: number, locale: Locale): string {
  const digits = Math.abs(usd) < 1 ? 4 : 2;
  return numberFormat(
    `${locale} usd ${digits}`,
    () =>
      new Intl.NumberFormat(locale, {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }),
  ).format(usd);
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

const clockFormats = new Map<string, { time: Intl.DateTimeFormat; day: Intl.DateTimeFormat }>();

/**
 * A time of day `HH:MM` in the user's zone, prefixed with the date when it is not today (a release time, "continues at
 * 14:05", ADR-081 section 5).
 */
export function formatClock(epochMs: number, { locale, timeZone, now }: TimeOptions): string {
  const key = `${locale} ${timeZone}`;
  let formats = clockFormats.get(key);
  if (formats === undefined) {
    formats = {
      time: new Intl.DateTimeFormat(locale, { timeZone, hourCycle: 'h23', hour: '2-digit', minute: '2-digit' }),
      day: timeFormat(locale, timeZone).day,
    };
    clockFormats.set(key, formats);
  }
  const date = formats.day.format(epochMs);
  return date === formats.day.format(now) ? formats.time.format(epochMs) : `${date} ${formats.time.format(epochMs)}`;
}

let zone: string | null = null;

/** The user's time zone (06-web.md section 12.4), resolved once per page load instead of once per shown time. */
export function localTimeZone(): string {
  zone ??= Intl.DateTimeFormat().resolvedOptions().timeZone;
  return zone;
}
