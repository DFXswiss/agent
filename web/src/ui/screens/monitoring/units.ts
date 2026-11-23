// Value formatters of the monitoring screen with the active locale (core/monitoring/units.ts) and the unit words of
// the message catalog.
import { formatEventTime, formatMoney, formatNumber } from '../../../core/i18n';
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

export function useUnits(): Units {
  const { t, locale } = useI18n();
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const units: Omit<Units, 'of'> = {
    bytes: (value) => formatBytes(value, locale),
    rate: (value) => formatRate(value, locale),
    cores: (value) => t('monitoring.unit.cores', { value: formatCores(value, locale) }),
    percent: (value) => formatPercent(value, locale),
    duration: formatDuration,
    money: (usd) => formatMoney(usd, locale),
    number: (value) => formatNumber(value, locale),
    time: (epochMs) => formatEventTime(epochMs, { locale, timeZone, now: Date.now() }),
    tick: (seconds, spanSeconds) =>
      new Intl.DateTimeFormat(locale, {
        timeZone,
        hourCycle: 'h23',
        hour: '2-digit',
        minute: '2-digit',
        ...(spanSeconds > 86_400 && { day: '2-digit', month: '2-digit' }),
      }).format(seconds * 1000),
  };
  const byUnit = { bytes: units.bytes, rate: units.rate, cores: units.cores };
  return { ...units, of: (unit) => byUnit[unit] };
}
