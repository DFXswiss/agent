// Value formatters of the monitoring screen with the active locale (core/monitoring/units.ts) and the unit words of
// the message catalog. The formatters are made once per locale and catalog (every row of the tree uses them) and the
// time zone is read once per page load.
import { useMemo } from 'react';
import { formatEventTime, formatMoney, formatNumber, type I18n, localTimeZone } from '../../../core/i18n';
import type { Locale } from '../../../core/i18n/locale';
import { formatBytes, formatCores, formatDuration, formatPercent, formatRate } from '../../../core/monitoring/units';
import type { ChartUnit } from '../../../core/monitoring/series';
import { useI18n } from '../../i18n';

export interface Units {
  readonly bytes: (value: number) => string;
  readonly rate: (value: number) => string;
  readonly cores: (value: number) => string;
  readonly percent: (value: number) => string;
  readonly duration: (seconds: number) => string;
  readonly money: (usd: number) => string;
  readonly number: (value: number) => string;
  readonly time: (epochMs: number) => string;
  /** An axis tick (unix seconds) of a chart spanning `spanSeconds`: hours and minutes, with the date beyond a day. */
  readonly tick: (seconds: number, spanSeconds: number) => string;
  readonly of: (unit: ChartUnit) => (value: number) => string;
}

/** Axis tick formats per locale, time zone and span (with the date beyond a day). */
const ticks = new Map<string, Intl.DateTimeFormat>();

function tickFormat(locale: Locale, timeZone: string, long: boolean): Intl.DateTimeFormat {
  const key = `${locale} ${timeZone} ${String(long)}`;
  let format = ticks.get(key);
  if (format === undefined) {
    format = new Intl.DateTimeFormat(locale, {
      timeZone,
      hourCycle: 'h23',
      hour: '2-digit',
      minute: '2-digit',
      ...(long && { day: '2-digit', month: '2-digit' }),
    });
    ticks.set(key, format);
  }
  return format;
}

export function useUnits(): Units {
  const { t, locale } = useI18n();
  return useMemo(() => makeUnits(t, locale), [t, locale]);
}

function makeUnits(t: I18n['t'], locale: Locale): Units {
  const timeZone = localTimeZone();
  const units: Omit<Units, 'of'> = {
    bytes: (value) => formatBytes(value, locale),
    rate: (value) => formatRate(value, locale),
    cores: (value) => t('monitoring.unit.cores', { value: formatCores(value, locale) }),
    percent: (value) => formatPercent(value, locale),
    duration: formatDuration,
    money: (usd) => formatMoney(usd, locale),
    number: (value) => formatNumber(value, locale),
    time: (epochMs) => formatEventTime(epochMs, { locale, timeZone, now: Date.now() }),
    tick: (seconds, spanSeconds) => tickFormat(locale, timeZone, spanSeconds > 86_400).format(seconds * 1000),
  };
  const byUnit = { bytes: units.bytes, rate: units.rate, cores: units.cores };
  return { ...units, of: (unit) => byUnit[unit] };
}
