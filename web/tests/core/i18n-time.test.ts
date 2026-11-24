// U-WEB-i18n-time (HOT-05): the event time formats are created once per locale and time zone, and the user's time
// zone is resolved once per page load.
import { describe, expect, it, vi } from 'vitest';
import { formatEventTime, localTimeZone } from '../../src/core/i18n';

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
