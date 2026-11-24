// Tab "Chat" of a task (06-web.md section 5.2): the usage slot, the timeline (task events by time, system rows,
// countdowns), the queue of waiting instructions (ADR-083, `PromptQueue.tsx`) and – for writers – the composer with the
// session controls; readers see "You have read access" and no controls (section 1: controls the rights do not allow
// are not rendered). A main session that failed for want of model access shows the card that starts the connection
// (`ModelAccessCard`, ADR-079).
import { useMemo } from 'react';
import { loadChat } from '../../../core/chat/actions';
import { selectTimeline } from '../../../core/chat/timeline';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useOrgView } from '../../screens/tasks/hooks';
import type { TaskTabProps } from '../../registries/task-tabs';
import { ChatComposer } from './ChatComposer';
import { ModelAccessCard } from './ModelAccessCard';
import { PromptQueue } from './PromptQueue';
import { Timeline } from './Timeline';
import { UsageSlot } from './UsageSlot';

function ChatView({ task }: { readonly task: TaskRow }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const select = useMemo(() => selectTimeline(task.wg_id, task.task_id), [task.wg_id, task.task_id]);
  const items = useStore(select, app.store);
  const load = useStore((state) => state.chat.loads[task.task_id] ?? null, app.store);
  const main = useStore(
    (state) => (task.main_session_id === null ? null : (state.tasks.sessions[task.main_session_id] ?? null)),
    app.store,
  );
  const { state, userId } = useOrgView(task.org_id);
  const writer = state !== null && userId !== null && state.canWriteWg(userId, task.wg_id);
  return (
    <section className="flex flex-col gap-4" aria-label={t('task.chat.label')} data-testid="chat">
      <UsageSlot orgId={task.org_id} taskId={task.task_id} />
      {load?.status === 'error' && (
        <p className="text-sm text-red-800" role="alert" data-testid="chat.error" data-code={load.code}>
          {t('task.chat.failed', { reason: error(load.code!).title })}{' '}
          <button
            type="button"
            className="min-h-11 underline"
            onClick={() => void loadChat(app.tasks, task.task_id)}
            data-testid="chat.reload"
          >
            {t('task.chat.reload')}
          </button>
        </p>
      )}
      <Timeline items={items} empty={t(load?.status === 'loading' ? 'task.chat.loading' : 'task.chat.empty')} />
      <ModelAccessCard task={task} main={main} userId={userId} />
      <PromptQueue task={task} writer={writer} />
      {writer ? (
        <ChatComposer task={task} main={main} />
      ) : (
        <p className="rounded bg-gray-100 p-3 text-sm" data-testid="chat.read-only">
          {t('task.chat.read-only')}
        </p>
      )}
    </section>
  );
}

export function ChatTab({ taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const task = useStore((state) => state.tasks.tasks[taskId] ?? null, app.store);
  return task === null ? null : <ChatView task={task} />;
}
