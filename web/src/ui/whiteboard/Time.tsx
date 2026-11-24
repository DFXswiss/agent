// A whiteboard or notification time as absolute local time `HH:MM:SS.mmm`, prefixed with the date when it is not today
// (06-web.md section 12.4; the time zone is resolved once per page load). `at` 0 means no usable time: nothing is shown.
import { formatEventTime, localTimeZone } from '../../core/i18n';
import { useNow } from '../countdown/useNow';
import { useI18n } from '../i18n';

export function Time({ at, testId }: { readonly at: number; readonly testId: string }) {
  const { locale } = useI18n();
  const now = useNow(false);
  if (at === 0) return null;
  return (
    <time className="text-xs text-gray-500" dateTime={new Date(at).toISOString()} data-testid={testId}>
      {formatEventTime(at, { locale, timeZone: localTimeZone(), now })}
    </time>
  );
}
