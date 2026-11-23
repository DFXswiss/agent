// The messages of the community (12-community.md section 6): oldest at the top, newest at the bottom, grouped by day;
// each with the display name, the sender mark, a "Moderator" badge for moderators and the receive time; own messages
// on the right. A removed message shows "Message removed", one that failed a check "Message could not be verified",
// one not opened yet (no key) a waiting note – never content. The message menu offers the actions the viewer has
// (`MessageActions`). The page follows new messages while it is at the bottom (and always after an own post).
import { Fragment, useEffect, useLayoutEffect, useRef } from 'react';
import type { Community } from '../../../core/community/engine';
import type { CommunityMessage } from '../../../core/store/slices/community';
import { useNow } from '../../countdown/useNow';
import { useI18n } from '../../i18n';
import { MessageActions } from './MessageActions';
import { dayKey, dayLabel, Mark, MessageText, Received } from './parts';

/** The page counts as "at the bottom" within this many CSS pixels. */
export const BOTTOM_SLACK = 120;

function atBottom(): boolean {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - BOTTOM_SLACK;
}

function Body({ message }: { readonly message: CommunityMessage }) {
  const { t } = useI18n();
  if (message.removed) {
    return (
      <p className="italic text-gray-600" data-testid="community.message.removed">
        {t('community.removed')}
      </p>
    );
  }
  if (message.status === 'verified') return <MessageText text={message.text!} />;
  const key = message.status === 'pending' ? 'pending' : 'unverifiable';
  return (
    <p className="italic text-gray-600" data-testid={`community.message.${key}`}>
      {t(`community.${key}`)}
    </p>
  );
}

function Message({
  message,
  me,
  moderator,
  engine,
  onNotice,
}: {
  readonly message: CommunityMessage;
  readonly me: string | null;
  readonly moderator: boolean;
  readonly engine: Community;
  readonly onNotice: (text: string) => void;
}) {
  const { t } = useI18n();
  const own = message.author === me;
  const name = message.name ?? t('community.unknown');
  return (
    <li
      className={`flex ${own ? 'justify-end' : 'justify-start'}`}
      data-testid="community.message"
      data-seq={message.seq}
      data-own={own}
      data-status={message.removed ? 'removed' : message.status}
    >
      <article
        className={`flex max-w-[85%] flex-col gap-1 rounded-lg px-3 py-2 md:max-w-[70%] ${own ? 'bg-blue-50' : 'bg-gray-100'}`}
      >
        <header className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {message.name !== null && (
            <span className="break-all font-semibold" data-testid="community.message.name">
              {message.name}
            </span>
          )}
          <Mark user={message.author} testId="community.message.mark" />
          {message.moderator && (
            <span
              className="rounded bg-purple-100 px-1.5 text-xs font-medium text-purple-900"
              data-testid="community.message.moderator"
            >
              {t('community.moderator')}
            </span>
          )}
          <Received at={message.at} testId="community.message.time" />
          <MessageActions
            message={message}
            name={name}
            own={own}
            moderator={moderator}
            engine={engine}
            onNotice={onNotice}
          />
        </header>
        <Body message={message} />
      </article>
    </li>
  );
}

export function Messages({
  messages,
  me,
  moderator,
  engine,
  onNotice,
}: {
  readonly messages: readonly CommunityMessage[];
  readonly me: string | null;
  readonly moderator: boolean;
  readonly engine: Community;
  readonly onNotice: (text: string) => void;
}) {
  const { t, locale } = useI18n();
  // the day labels ("Today") follow midnight
  const now = useNow(true, 60_000);
  const end = useRef<HTMLDivElement>(null);
  const newest = messages[messages.length - 1];
  const follow = useRef(true);
  const shown = useRef<number | null>(null);
  // whether the page was at the bottom before the newest message arrived
  useEffect(() => {
    const update = () => {
      follow.current = atBottom();
    };
    window.addEventListener('scroll', update, { passive: true });
    return () => window.removeEventListener('scroll', update);
  }, []);
  useLayoutEffect(() => {
    if (newest === undefined || newest.seq === shown.current) return;
    const first = shown.current === null;
    shown.current = newest.seq;
    if (first || follow.current || newest.author === me) end.current?.scrollIntoView?.({ block: 'end' });
  }, [newest, me]);
  const days: { key: string; at: number; items: CommunityMessage[] }[] = [];
  for (const message of messages) {
    const key = dayKey(message.at);
    const last = days[days.length - 1];
    if (last?.key === key) last.items.push(message);
    else days.push({ key, at: message.at, items: [message] });
  }
  return (
    <section aria-label={t('community.list.label')} className="flex flex-col gap-3" data-testid="community.messages">
      {days.map((day) => (
        <Fragment key={`${day.key}-${day.items[0]!.seq}`}>
          <h2
            className="sticky top-0 z-10 self-center rounded-full bg-white/90 px-3 py-0.5 text-xs font-medium text-gray-700 shadow-sm"
            data-testid="community.day"
          >
            {dayLabel(day.at, now, locale, t)}
          </h2>
          <ul className="flex flex-col gap-2">
            {day.items.map((message) => (
              <Message
                key={message.seq}
                message={message}
                me={me}
                moderator={moderator}
                engine={engine}
                onNotice={onNotice}
              />
            ))}
          </ul>
        </Fragment>
      ))}
      <div ref={end} />
    </section>
  );
}
