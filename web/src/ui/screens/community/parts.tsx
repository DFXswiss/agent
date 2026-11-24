// Building blocks of the community screen (14-community.md section 6): the sender mark (`#` and four base32
// characters of SHA-256 of the user ID, computed once per user), the receive time (`HH:MM`, date and time with seconds
// as tooltip and in `<time dateTime>`), the day of a group ("Today", "Yesterday", else the date) and the text of a
// message – line breaks kept, long words broken, `http(s)` links as links opening a new tab, never HTML.
import { useEffect, useState } from 'react';
import { segments } from '../../../core/community/links';
import { senderMark } from '../../../core/community/mark';
import type { Locale } from '../../../core/i18n';
import { useI18n } from '../../i18n';

const marks = new Map<string, string>();

/** The sender mark of `user` (empty until computed; cached for the page's lifetime). */
export function useMark(user: string): string {
  const [, setComputed] = useState(0);
  useEffect(() => {
    if (marks.has(user)) return;
    // a state update after unmounting is ignored (React 18)
    void senderMark(user).then((value) => {
      marks.set(user, value);
      setComputed((count) => count + 1);
    });
  }, [user]);
  return marks.get(user) ?? '';
}

export function Mark({ user, testId }: { readonly user: string; readonly testId: string }) {
  const { t } = useI18n();
  const mark = useMark(user);
  return (
    <span className="font-mono text-xs text-gray-600" title={t('community.mark.title')} data-testid={testId}>
      {mark}
    </span>
  );
}

/** The local calendar day of a time (`YYYY-MM-DD` in the browser's time zone), the key of a day group. */
export function dayKey(at: number): string {
  const date = new Date(at);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The heading of a day group: "Today", "Yesterday" or the full date. */
export function dayLabel(at: number, now: number, locale: Locale, t: (key: string) => string): string {
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (dayKey(at) === dayKey(now)) return t('community.today');
  if (dayKey(at) === dayKey(yesterday.getTime())) return t('community.yesterday');
  return new Intl.DateTimeFormat(locale, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(
    at,
  );
}

/** Date and time with seconds in the locale's form. */
export function fullTime(at: number, locale: Locale): string {
  return new Intl.DateTimeFormat(locale, {
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(at);
}

/** The receive time `HH:MM`, with the date and the time with seconds as tooltip. */
export function Received({ at, testId }: { readonly at: number; readonly testId: string }) {
  const { locale } = useI18n();
  if (at === 0) return null;
  const short = new Intl.DateTimeFormat(locale, { hourCycle: 'h23', hour: '2-digit', minute: '2-digit' }).format(at);
  const full = fullTime(at, locale);
  return (
    <time className="text-xs text-gray-600" dateTime={new Date(at).toISOString()} title={full} data-testid={testId}>
      {short}
    </time>
  );
}

/** The text of a verified message: plain text and links, line breaks kept, long words broken. */
export function MessageText({ text }: { readonly text: string }) {
  const { t } = useI18n();
  return (
    <p className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]" data-testid="community.message.text">
      {segments(text).map((segment, index) =>
        segment.kind === 'text' ? (
          segment.text
        ) : (
          <a
            key={index}
            href={segment.href}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="text-blue-800 underline"
            data-testid="community.message.link"
          >
            {segment.href}
            <span className="sr-only"> {t('community.link.external')}</span>
          </a>
        ),
      )}
    </p>
  );
}
