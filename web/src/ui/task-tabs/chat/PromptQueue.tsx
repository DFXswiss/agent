// The queue of a task above the composer (06-web.md section 5.2, ADR-083 section 3), for every reader: "N
// instructions waiting" (nothing while none waits), the waiting prompts in delivery order with their sender (the
// verified author, "you" for oneself, "(through their agent)" when the hub received the prompt from the author's
// agent) and their state - waiting, waiting for the device (the session's device is offline or there is none), being
// withdrawn. A writer withdraws their own prompts that are not already being withdrawn ("Withdraw"); a refusal is
// shown with its reason (`peer_outdated`: the device must be updated first). The queue is read when the chat opens
// and again after changes of the task, its sessions and its prompts (`core/chat/queue.ts`). The waiting prompts are a
// windowed list (`task.queue`, KOR-35, ADR-086 section 1): a task may have thousands queued.
import { createContext, memo, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { type QueueWatch, watchQueue, withdrawPrompt } from '../../../core/chat/queue';
import { requestErrorCode } from '../../../core/requests/client';
import type { QueuedPrompt } from '../../../core/store/slices/queue';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { type RowAttributes, WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { usePeople } from '../people';

interface Failure {
  readonly event: string;
  readonly code: string;
}

function Sender({ org, prompt }: { readonly org: string; readonly prompt: QueuedPrompt }) {
  const { t } = useI18n();
  const { me, name } = usePeople(org);
  let text: string;
  if (prompt.authorId === me) text = t(prompt.viaAgent ? 'queue.you.via-agent' : 'queue.you');
  else text = prompt.viaAgent ? t('queue.via-agent', { name: name(prompt.authorId) }) : name(prompt.authorId);
  return <span data-testid="chat.queue.author">{text}</span>;
}

/** Rows of the queue rendered at first and per move, and the cap. */
export const QUEUE_PAGE = 20;
export const QUEUE_MAX = 100;

interface QueueActions {
  readonly org: string;
  readonly writer: boolean;
  readonly me: string | null;
  readonly busy: string | null;
  readonly failure: Failure | null;
  readonly withdraw: (event: string) => void;
}

const QueueContext = createContext<QueueActions | null>(null);

const promptKey = (prompt: QueuedPrompt): string => prompt.eventId;

const promptAttributes = (prompt: QueuedPrompt): RowAttributes => ({
  className: 'flex flex-wrap items-center gap-2',
  'data-testid': 'chat.queue.item',
  'data-event': prompt.eventId,
  'data-state': prompt.state,
  'data-author': prompt.authorId,
});

function QueueRow({ item: prompt }: { readonly item: QueuedPrompt }) {
  const { t, error } = useI18n();
  const { org, writer, me, busy, failure, withdraw } = useContext(QueueContext)!;
  return (
    <>
      <Sender org={org} prompt={prompt} />
      <span className="rounded bg-gray-100 px-2 text-xs text-gray-800" data-testid="chat.queue.state">
        {t(`queue.state.${prompt.state}`)}
      </span>
      {writer && prompt.authorId === me && prompt.state !== 'withdrawing' && (
        <button
          type="button"
          className="min-h-11 rounded border border-gray-400 px-3"
          disabled={busy !== null}
          onClick={() => withdraw(prompt.eventId)}
          data-testid="chat.queue.withdraw"
        >
          {t(busy === prompt.eventId ? 'queue.withdrawing' : 'queue.withdraw')}
        </button>
      )}
      {failure?.event === prompt.eventId && (
        <p className="basis-full text-red-800" role="alert" data-testid="chat.queue.error" data-code={failure.code}>
          {failure.code === 'peer_outdated'
            ? t('queue.error.peer_outdated')
            : t('queue.error', { reason: error(failure.code).title })}
        </p>
      )}
    </>
  );
}

interface PromptQueueProps {
  readonly task: TaskRow;
  readonly writer: boolean;
  /** The number of prompts in the chat's timeline (another number reads the queue again). */
  readonly prompts: number;
}

export const PromptQueue = memo(function PromptQueue({ task, writer, prompts }: PromptQueueProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const queue = useStore((state) => state.queue.byTask[task.task_id] ?? null, app.store);
  const me = useStore((state) => state.me.userId, app.store);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const watch = useRef<QueueWatch | null>(null);
  useEffect(() => {
    const started = watchQueue(app.tasks, task.task_id);
    watch.current = started;
    return started.stop;
  }, [app, task.task_id]);
  useEffect(() => watch.current!.prompts(prompts), [prompts]);
  const actions = useMemo<QueueActions>(
    () => ({
      org: task.org_id,
      writer,
      me,
      busy,
      failure,
      withdraw: (event: string) => {
        setBusy(event);
        setFailure(null);
        withdrawPrompt(app.tasks, task.task_id, event)
          .catch((caught: unknown) => setFailure({ event, code: requestErrorCode(caught) }))
          .finally(() => setBusy(null));
      },
    }),
    [app, task.org_id, task.task_id, writer, me, busy, failure],
  );
  if (queue === null || queue.prompts.length === 0) return null;
  const count = queue.prompts.length;
  return (
    <section
      className="flex flex-col gap-2 rounded border border-gray-200 p-3"
      aria-labelledby="chat-queue-heading"
      data-testid="chat.queue"
      data-count={count}
    >
      <h3 id="chat-queue-heading" className="text-sm font-semibold" data-testid="chat.queue.heading">
        {t(`queue.heading.${new Intl.PluralRules(locale).select(count)}`, { count })}
      </h3>
      <QueueContext.Provider value={actions}>
        <WindowedList
          listId="task.queue"
          items={queue.prompts}
          getKey={promptKey}
          Row={QueueRow}
          newest="start"
          size={QUEUE_PAGE}
          page={QUEUE_PAGE}
          maxRows={QUEUE_MAX}
          className="flex flex-col gap-2 text-sm"
          rowAttributes={promptAttributes}
          rowHeight="sm"
          testId="chat.queue.list"
        />
      </QueueContext.Provider>
    </section>
  );
});
