// U-WEB-i18n-time (HOT-05): the event time formats are created once per locale and time zone, and the user's time
// zone is resolved once per page load.
import { describe, expect, it, vi } from 'vitest';
import { formatClock, formatEventTime, localTimeZone } from '../../src/core/i18n';

describe('event time', () => {
  it('reuses its formats and resolves the time zone once', () => {
    const resolved = vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions');
    const zone = localTimeZone();
    expect(localTimeZone()).toBe(zone);
    expect(resolved).toHaveBeenCalledTimes(1);
    const options = { locale: 'en' as const, timeZone: 'Europe/Zurich', now: Date.UTC(2026, 10, 21, 12) };
    const at = Date.UTC(2026, 10, 21, 12, 4, 5, 6);
    expect(formatEventTime(at, options)).toBe('13:04:05.006');
    expect(formatEventTime(at - 86_400_000, options)).toBe('11/20/2026 13:04:05.006');
  });
});

describe('clock time', () => {
  it('shows HH:MM today and the date before it on another day, with its formats reused', () => {
    const options = { locale: 'de' as const, timeZone: 'Europe/Zurich', now: Date.UTC(2026, 10, 24, 12) };
    const at = Date.UTC(2026, 10, 24, 13, 5, 30);
    expect(formatClock(at, options)).toBe('14:05');
    expect(formatClock(at, options)).toBe('14:05');
    expect(formatClock(at + 86_400_000, options)).toBe('25.11.2026 14:05');
  });
});
