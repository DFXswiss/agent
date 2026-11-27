// Tab "Claude Code" of a task (06-web.md section 5.2a, KONZEPT E-43): the task looks like Claude Code started locally.
// The native terminal of the main session (the read-only xterm view of ADR-016, loaded lazily) fills the page; below it
// the prompt input in the style of Claude Code (`ChatComposer` variant `cli`: Enter sends, Shift+Enter a new line).
// A prompt is still a signed `prompt` event delivered by the runner (ADR-025) – there is no keystroke path into the
// terminal, KONZEPT §6 P7 holds. Until the main session works (or after it failed) the countdown panel of its start
// sits above the terminal (06-web.md section 4.4, with "Retry" for writers); before the main session exists a dark
// placeholder stands in for the terminal. After a resume or failover the view follows the new main session. The model access card and the queue of waiting
// instructions appear here as in the chat; readers see "You have read access" and no input.
import { lazy, Suspense, useMemo } from 'react';
import { promptCount } from '../../../core/chat/queue';
import { selectTimeline } from '../../../core/chat/timeline';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { settled } from '../../../core/tasks/countdown';
import { retryPrompt } from '../../../core/tasks/send';
import { CountdownPanel } from '../../countdown/CountdownPanel';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { ChunkBoundary } from '../../lazy/ChunkBoundary';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useOrgView } from '../../screens/tasks/hooks';
import { useAppEnv } from '../../shell/context';
import { ChatComposer } from '../chat/ChatComposer';
import { ModelAccessCard } from '../chat/ModelAccessCard';
import { PromptQueue } from '../chat/PromptQueue';

const TerminalView = lazy(() => import('../../terminal/TerminalView'));

function Placeholder({ text, testId }: { readonly text: string; readonly testId: string }) {
  return (
    <p
      className="flex min-h-64 items-center justify-center rounded bg-black p-4 font-mono text-sm text-gray-300"
      data-testid={testId}
    >
      {text}
    </p>
  );
}

function TerminalTaskView({ task }: { readonly task: TaskRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const select = useMemo(() => selectTimeline(task.wg_id, task.task_id), [task.wg_id, task.task_id]);
  const items = useStore(select, app.store);
  const main = useStore(
    (state) => (task.main_session_id === null ? null : (state.tasks.sessions[task.main_session_id] ?? null)),
    app.store,
  );
  const { state, userId } = useOrgView(task.org_id);
  const writer = state !== null && userId !== null && state.canWriteWg(userId, task.wg_id);
  const session = task.main_session_id;
  // the start of the main session (countdown, steps, waiting reason, Retry) stays visible until it works or failed
  const failed = main === null ? task.state === 'failed' : main.row.state === 'failed';
  const starting = !settled(task, main) || failed;
  return (
    <section className="flex flex-col gap-3" aria-label={t('task.terminal.label')} data-testid="task.terminal">
      {starting &&
        (writer ? (
          <CountdownPanel
            task={task}
            onRetry={async () => {
              await retryPrompt(app.tasks, task);
            }}
          />
        ) : (
          <CountdownPanel task={task} />
        ))}
      {session === null ? (
        <Placeholder text={t('task.terminal.starting')} testId="task.terminal.starting" />
      ) : (
        <ChunkBoundary
          fallback={
            <p className="text-sm text-red-800" role="alert" data-testid="terminal.chunk-unavailable">
              {t('terminal.chunk-unavailable')}
            </p>
          }
        >
          <Suspense fallback={<Placeholder text={t('terminal.loading')} testId="terminal.loading" />}>
            <TerminalView key={session} org={task.org_id} wg={task.wg_id} session={session} variant="task" />
          </Suspense>
        </ChunkBoundary>
      )}
      <ModelAccessCard task={task} main={main} userId={userId} />
      <PromptQueue task={task} writer={writer} prompts={promptCount(items)} />
      {writer ? (
        <ChatComposer task={task} main={main} variant="cli" />
      ) : (
        <p className="rounded bg-gray-100 p-3 text-sm" data-testid="chat.read-only">
          {t('task.chat.read-only')}
        </p>
      )}
    </section>
  );
}

export function TerminalTab({ taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const task = useStore((state) => state.tasks.tasks[taskId] ?? null, app.store);
  return task === null ? null : <TerminalTaskView task={task} />;
}
