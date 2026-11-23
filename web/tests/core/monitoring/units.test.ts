// U-WEB-monitoring-units: values with units (06-web.md section 9.1, 08-monitoring.md section 6).
import { describe, expect, it } from 'vitest';
import {
  formatBytes,
  formatCores,
  formatDuration,
  formatPercent,
  formatRate,
  share,
} from '../../../src/core/monitoring/units';

describe('units', () => {
  it('formats bytes in binary units with one decimal below ten', () => {
    expect(formatBytes(0, 'en')).toBe('0 B');
    expect(formatBytes(1023, 'en')).toBe('1,023 B');
    expect(formatBytes(1536, 'en')).toBe('1.5 KiB');
    expect(formatBytes(20 * 1024 * 1024, 'en')).toBe('20 MiB');
    expect(formatBytes(1.25 * 1024 ** 3, 'de')).toBe('1,3 GiB');
    expect(formatBytes(3 * 1024 ** 6, 'en')).toBe('3,072 PiB');
  });

  it('formats rates, cores and percent', () => {
    expect(formatRate(2048, 'en')).toBe('2.0 KiB/s');
    expect(formatCores(0.5, 'en')).toBe('0.50');
    expect(formatCores(1.234, 'de')).toBe('1,23');
    expect(formatPercent(0.4567, 'en')).toBe('45.7%');
    expect(formatPercent(1, 'de')).toBe('100\u00a0%');
  });

  it('formats durations with the two largest units', () => {
    expect(formatDuration(0)).toBe('0 s');
    expect(formatDuration(-5)).toBe('0 s');
    expect(formatDuration(45.9)).toBe('45 s');
    expect(formatDuration(125)).toBe('2 min 5 s');
    expect(formatDuration(7500)).toBe('2 h 5 min');
    expect(formatDuration(3 * 86_400 + 4 * 3600 + 59)).toBe('3 d 4 h');
    expect(formatDuration(86_400 + 60)).toBe('1 d');
  });

  it('computes shares only with a positive limit', () => {
    expect(share(1, 4)).toBe(0.25);
    expect(share(null, 4)).toBeNull();
    expect(share(1, null)).toBeNull();
    expect(share(1, 0)).toBeNull();
  });
});
