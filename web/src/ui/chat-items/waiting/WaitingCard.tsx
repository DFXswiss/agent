// The waiting card in a task's chat (06-web.md section 5.2, KOR-25, ADR-066): a `session.status` event of the state
// `waiting` (folded per reason and addressee by `core/github/waiting.ts`) asks the person who gave the instruction
// (`for_user`, HOT-14; the task owner for a body without it) to connect GitHub so that a held operation goes on – a
// personal connection is never used on behalf of another person. That person connects right in the card (device flow or an existing token, the same `GithubConnect`
// as the settings section; it stacks on a phone). A stored connection older than the waiting item is the one the
// runner could not use, so the card says "connect again". There is no approval card: a repository outside a
// workgroup's optional list is refused as before (ADR-054).
//
// Everybody else – the task owner too – sees "waiting for <name>" and no control. The card shows only the platform's own texts, the
// operation type (its catalog label, a known type or the dotted type the contract's pattern checked) and the repository
// name the contract's pattern checked – never text of the session. It turns into a short "done" row once GitHub is
// connected or every operation of the card is registered, into "expired" after `until`.
import { useEffect, useMemo } from 'react';
import { loadGithub } from '../../../core/github/connection';
import { waitStatus, type WaitingCard as Card } from '../../../core/github/waiting';
import type { TimelineItem } from '../../../core/chat/timeline';
import { ensureOperations } from '../../../core/operations/load';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useDirectory, useOrgView, useOwner, useTitles } from '../../screens/tasks/hooks';
import { GithubConnect } from '../../settings/sections/github/Connect';
import { useAppEnv } from '../../shell/context';
import { EventTime } from '../../task-tabs/chat/EventTime';
import { useTypeLabel } from '../operation/Card';
import { knownPersonName } from '../../names/names';

function TaskCard({ item, card, task }: { readonly item: TimelineItem; readonly card: Card; readonly task: TaskRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const typeLabel = useTypeLabel();
  const { state, userId } = useOrgView(task.org_id);
  const tasks = useMemo(() => [task], [task]);
  useTitles(tasks);
  const owner = useOwner(task);
  const directory = useDirectory(task.org_id, state !== null);
  const addressee = card.forUser ?? owner;
  const isOwner = addressee !== null && addressee === userId;
  const github = useStore((store) => store.github, app.store);
  const rows = useStore((store) => store.operations.rows, app.store);
  const now = useNow(true, 1000);
  useEffect(() => {
    ensureOperations(app.tasks, task.task_id).catch(() => undefined);
  }, [app, task.task_id]);
  useEffect(() => {
    if (isOwner && github.status === 'unknown') loadGithub(app.tasks).catch(() => undefined);
  }, [app, isOwner, github.status]);
  const created = isOwner && github.status === 'connected' ? Date.parse(github.createdAt) : NaN;
  const connectedAt = Number.isNaN(created) ? null : created;
  const status = waitStatus(card, { now, connectedAt, rows });
  if (status !== 'open')
    return (
      <p
        className="text-center text-xs text-gray-600"
        role="status"
        data-testid="waiting.card"
        data-reason={card.reason}
        data-status={status}
      >
        {t(`github.waiting.${status}.connection`)} <EventTime at={item.at} />
      </p>
    );
  const ownerName =
    knownPersonName(t, directory, addressee) ??
    t(card.forUser === null ? 'github.waiting.owner.unknown' : 'github.waiting.person.unknown');
  const stale = github.status === 'unverifiable' || connectedAt !== null;
  return (
    <section
      className="flex min-w-0 flex-col gap-3 rounded border border-amber-300 bg-amber-50 p-3 sm:p-4"
      aria-label={t('github.waiting.label')}
      data-testid="waiting.card"
      data-reason={card.reason}
      data-status={status}
      data-owner={isOwner}
    >
      <h3 className="font-semibold" data-testid="waiting.title">
        {t('github.waiting.connection.title')}
      </h3>
      {isOwner ? (
        <p className="text-sm">{t('github.waiting.connection.body')}</p>
      ) : (
        <p className="text-sm" data-testid="waiting.other">
          {t('github.waiting.other.connection', { owner: ownerName })}
        </p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-2 text-sm">
        <dt className="text-gray-700">{t('github.waiting.operation')}</dt>
        <dd className="break-words" data-testid="waiting.operation" data-type={card.operationType}>
          {typeLabel(card.operationType)}
        </dd>
        {card.repo !== null && (
          <>
            <dt className="text-gray-700">{t('github.waiting.repo')}</dt>
            <dd className="break-all font-mono" data-testid="waiting.repo">
              {card.repo}
            </dd>
          </>
        )}
      </dl>
      <p className="text-xs text-gray-700" data-testid="waiting.until">
        {card.operations.length > 1 && `${t('github.waiting.count', { count: card.operations.length })} `}
        {t('github.waiting.until', { minutes: Math.max(1, Math.ceil((card.until - now) / 60_000)) })}{' '}
        <EventTime at={item.at} />
      </p>
      {isOwner && (
        <>
          {stale && (
            <p className="text-sm" data-testid="waiting.reconnect">
              {t('github.waiting.connection.reconnect')}
            </p>
          )}
          <GithubConnect />
        </>
      )}
    </section>
  );
}

/** The card of a folded waiting item; null while the item's task is not in the store. */
export function WaitingCard({ item, card }: { readonly item: TimelineItem; readonly card: Card }) {
  const { app } = useAppEnv();
  const task = useStore((store) => {
    const id = store.tasks.sessions[item.sessionId ?? '']?.row.task_id;
    return id === undefined ? undefined : store.tasks.tasks[id];
  }, app.store);
  return task === undefined ? null : <TaskCard item={item} card={card} task={task} />;
}
