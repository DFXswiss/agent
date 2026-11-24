// The queue of a task above the composer (06-web.md section 5.2, ADR-083 section 3), for every reader: "N
// instructions waiting" (nothing while none waits), the waiting prompts in delivery order with their sender (the
// verified author, "you" for oneself, "(through their agent)" when the hub received the prompt from the author's
// agent) and their state - waiting, waiting for the device (the session's device is offline or there is none), being
// withdrawn. A writer withdraws their own prompts that are not already being withdrawn ("Withdraw"); a refusal is
// shown with its reason (`peer_outdated`: the device must be updated first). The queue is read when the chat opens
// and again after changes of the task, its sessions and its prompts (`core/chat/queue.ts`).
import { useEffect, useState } from 'react';
import { watchQueue, withdrawPrompt } from '../../../core/chat/queue';
import { requestErrorCode } from '../../../core/requests/client';
import type { QueuedPrompt } from '../../../core/store/slices/queue';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
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

export function PromptQueue({ task, writer }: { readonly task: TaskRow; readonly writer: boolean }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const queue = useStore((state) => state.queue.byTask[task.task_id] ?? null, app.store);
  const me = useStore((state) => state.me.userId, app.store);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  useEffect(() => watchQueue(app.tasks, task.task_id), [app, task.task_id]);
  if (queue === null || queue.prompts.length === 0) return null;
  const count = queue.prompts.length;
  const withdraw = (event: string) => {
    setBusy(event);
    setFailure(null);
    withdrawPrompt(app.tasks, task.task_id, event)
      .catch((caught: unknown) => setFailure({ event, code: requestErrorCode(caught) }))
      .finally(() => setBusy(null));
  };
  return (
    <section
      className="flex flex-col gap-2 rounded border border-gray-200 p-3"
      aria-labelledby="chat-queue-heading"
      data-testid="chat.queue"
      data-count={count}
    >
      <h3 id="chat-queue-heading" className="text-sm font-semibold" data-testid="chat.queue.heading">
        {count === 1 ? t('queue.heading.one') : t('queue.heading.other', { count })}
      </h3>
      <ol className="flex flex-col gap-2 text-sm">
        {queue.prompts.map((prompt) => (
          <li
            key={prompt.eventId}
            className="flex flex-wrap items-center gap-2"
            data-testid="chat.queue.item"
            data-event={prompt.eventId}
            data-state={prompt.state}
            data-author={prompt.authorId}
          >
            <Sender org={task.org_id} prompt={prompt} />
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
              <p
                className="basis-full text-red-800"
                role="alert"
                data-testid="chat.queue.error"
                data-code={failure.code}
              >
                {failure.code === 'peer_outdated'
                  ? t('queue.error.peer_outdated')
                  : t('queue.error', { reason: error(failure.code).title })}
              </p>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
