// Tab "Claude Code" of a task (06-web.md section 5.2a, KONZEPT E-43, ADR-104): in the browser the task looks like Claude
// Code started locally. The real TUI of the main session (the read-only xterm view of ADR-016, variant `task`, loaded
// lazily) fills the page; the one input (`ClaudeInput`) lies exactly over the TUI's own input box, so there are never
// two inputs; while the screen shows no box (the start, an agent that ended) the input is docked below the screen.
// Prompts stay signed `prompt` events delivered by the runner (ADR-025); no keystroke reaches the terminal
// (KONZEPT §6 P7). Until the main session works – and after its start failed – the start is shown as terminal lines
// above the screen (`TerminalStart`, with "Retry" for writers). After a resume or failover the view follows the new main
// session. The model access card and the queue of waiting instructions appear as in the chat; readers see the TUI as
// it is (its own box included) and a quiet "You have read access".
import { lazy, Suspense, useMemo, useState } from 'react';
import { promptCount } from '../../../core/chat/queue';
import { selectTimeline } from '../../../core/chat/timeline';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { settled } from '../../../core/tasks/countdown';
import { retryPrompt } from '../../../core/tasks/send';
import { THEME } from '../../../core/terminal/screen';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { ChunkBoundary } from '../../lazy/ChunkBoundary';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useOrgView } from '../../screens/tasks/hooks';
import { useAppEnv } from '../../shell/context';
import type { TerminalLayout } from '../../terminal/TerminalView';
import { useComposer } from '../chat/ChatComposer';
import { ModelAccessCard } from '../chat/ModelAccessCard';
import { PromptQueue } from '../chat/PromptQueue';
import { ClaudeInput, RULE_COLOR } from './ClaudeInput';
import { TerminalStart } from './TerminalStart';

const TerminalView = lazy(() => import('../../terminal/TerminalView'));

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
  const composer = useComposer(task, main);
  const [layout, setLayout] = useState<TerminalLayout | null>(null);
  const session = task.main_session_id;
  // the start of the main session stays visible until it works or failed
  const failed = main === null ? task.state === 'failed' : main.row.state === 'failed';
  const starting = !settled(task, main) || failed;
  const boxed = layout !== null && layout.box !== null;
  const muted = 'px-2 font-mono text-sm';
  return (
    <section
      className="flex min-h-0 flex-1 flex-col"
      style={{ background: THEME.background, color: THEME.foreground }}
      aria-label={t('task.terminal.label')}
      data-testid="task.terminal"
      data-boxed={boxed}
    >
      {starting &&
        (writer ? (
          <TerminalStart
            task={task}
            onRetry={async () => {
              await retryPrompt(app.tasks, task);
            }}
          />
        ) : (
          <TerminalStart task={task} />
        ))}
      <ModelAccessCard task={task} main={main} userId={userId} />
      {session === null ? (
        <div className="min-h-0 flex-1" data-testid="task.terminal.waiting" />
      ) : (
        <ChunkBoundary
          fallback={
            <p
              className={muted}
              style={{ color: THEME.brightRed }}
              role="alert"
              data-testid="terminal.chunk-unavailable"
            >
              {t('terminal.chunk-unavailable')}
            </p>
          }
        >
          <Suspense
            fallback={
              <p className={`${muted} flex-1`} style={{ color: RULE_COLOR }} data-testid="terminal.loading">
                {t('terminal.loading')}
              </p>
            }
          >
            <TerminalView
              key={session}
              org={task.org_id}
              wg={task.wg_id}
              session={session}
              variant="task"
              onLayout={setLayout}
            >
              {writer && boxed && <ClaudeInput composer={composer} placement="box" layout={layout} />}
            </TerminalView>
          </Suspense>
        </ChunkBoundary>
      )}
      <PromptQueue task={task} writer={writer} prompts={promptCount(items)} />
      {writer && !boxed && <ClaudeInput composer={composer} placement="docked" layout={layout} />}
      {!writer && (
        <p className={muted} style={{ color: RULE_COLOR }} data-testid="chat.read-only">
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
