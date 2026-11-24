// The messages of the community (14-community.md section 6): oldest at the top, newest at the bottom, grouped by day;
// each with the display name, the sender mark, a "Moderator" badge for moderators and the receive time; own messages
// on the right. A removed message shows "Message removed", one that failed a check "Message could not be verified",
// one not opened yet (no key) a waiting note – never content. The message menu offers the actions the viewer has
// (`MessageActions`). The page follows new messages while it is at the bottom (and always after an own post).
//
// The messages are a `WindowedList` in follow mode at the end (ADR-086 section 1): `WINDOW` rows at first, earlier
// loaded ones `PAGE` at a time, at most `MAX_ROWS` rendered; the first message of a day carries the day label.
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import type { Community } from '../../../core/community/engine';
import type { CommunityMessage } from '../../../core/store/slices/community';
import { useNow } from '../../countdown/useNow';
import { useI18n } from '../../i18n';
import { type RowAttributes, type RowProps, WindowedList } from '../../lists/WindowedList';
import { MessageActions } from './MessageActions';
import { dayKey, dayLabel, Mark, MessageText, Received } from './parts';

/** Rows rendered at first, rows added by "Show earlier", the cap. */
export const WINDOW = 100;
export const PAGE = 50;
export const MAX_ROWS = 200;

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

/** One row: the message, whether it starts a day (its day label), and what its menu needs. */
interface Item {
  readonly message: CommunityMessage;
  readonly firstOfDay: boolean;
  readonly me: string | null;
  readonly moderator: boolean;
  readonly engine: Community;
  readonly onNotice: (text: string) => void;
}

const sameItem = (a: Item, b: Item): boolean =>
  a.message === b.message && a.firstOfDay === b.firstOfDay && a.me === b.me && a.moderator === b.moderator;

const itemKey = (item: Item): string => String(item.message.seq);

function attributes({ message, me }: Item): RowAttributes {
  const own = message.author === me;
  return {
    className: `flex flex-col gap-2 ${own ? 'items-end' : 'items-start'}`,
    'data-testid': 'community.message',
    'data-seq': message.seq,
    'data-own': own,
    'data-status': message.removed ? 'removed' : message.status,
  };
}

function DayLabel({ at }: { readonly at: number }) {
  const { t, locale } = useI18n();
  // the day labels ("Today") follow midnight
  const now = useNow(true, 60_000);
  return (
    <h2
      className="self-center rounded-full bg-white/90 px-3 py-0.5 text-xs font-medium text-gray-700 shadow-sm"
      data-testid="community.day"
    >
      {dayLabel(at, now, locale, t)}
    </h2>
  );
}

function Message({ item }: RowProps<Item>) {
  const { message, me, moderator, engine, onNotice } = item;
  const { t } = useI18n();
  const own = message.author === me;
  const name = message.name ?? t('community.unknown');
  return (
    <>
      {item.firstOfDay && <DayLabel at={message.at} />}
      <article
        className={`flex max-w-[85%] flex-col gap-1 rounded-lg px-3 py-2 md:max-w-[70%] ${own ? 'bg-blue-50' : 'bg-gray-100'}`}
      >
        <header className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          {message.name !== null && (
            <span className="break-all font-semibold" data-testid="community.message.name">
              {message.agent === true ? t('community.agent-of', { name: message.name }) : message.name}
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
    </>
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
  const { t } = useI18n();
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
  const items = useMemo(
    () =>
      messages.map((message, index): Item => ({
        message,
        firstOfDay: index === 0 || dayKey(messages[index - 1]!.at) !== dayKey(message.at),
        me,
        moderator,
        engine,
        onNotice,
      })),
    [messages, me, moderator, engine, onNotice],
  );
  return (
    <section aria-label={t('community.list.label')} className="flex flex-col gap-3" data-testid="community.messages">
      <WindowedList
        listId="community.messages"
        items={items}
        getKey={itemKey}
        Row={Message}
        same={sameItem}
        newest="end"
        size={WINDOW}
        page={PAGE}
        maxRows={MAX_ROWS}
        className="flex flex-col gap-2"
        rowAttributes={attributes}
        testId="community.list"
      />
      <div ref={end} />
    </section>
  );
}
