// Chat item `countdown` (06-web.md sections 4.4 and 5.2): the start of a main session. For the task's current main
// session the live countdown panel (digits, steps, "Delayed", reasons, "Ready in", the failure with "Retry" for
// writers); for an earlier main session (a follow-up restarted the task) the one-line summary.
import type { TimelineItem } from '../../core/chat/timeline';
import { countdown } from '../../core/tasks/countdown';
import { retryPrompt } from '../../core/tasks/send';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { CountdownPanel } from '../countdown/CountdownPanel';
import { useSeconds } from '../countdown/parts';
import { useNow } from '../countdown/useNow';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { useOrgView } from '../screens/tasks/hooks';

function Countdown({ item }: ChatItemProps) {
  const { sessionId, body } = item as TimelineItem;
  const taskId = (body as { readonly task: string }).task;
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const seconds = useSeconds();
  const now = useNow(false);
  const session = useStore((state) => state.tasks.sessions[sessionId!] ?? null, app.store);
  const task = useStore((state) => state.tasks.tasks[taskId] ?? null, app.store);
  const { state, userId } = useOrgView(task?.org_id ?? '');
  if (task === null || session === null) return null;
  if (task.main_session_id === sessionId) {
    const writer = state !== null && userId !== null && state.canWriteWg(userId, task.wg_id);
    return writer ? (
      <CountdownPanel
        task={task}
        onRetry={async () => {
          await retryPrompt(app.tasks, task);
        }}
      />
    ) : (
      <CountdownPanel task={task} />
    );
  }
  const view = countdown({ task, session, launch: null, hubOffsetMs: null, now });
  let text: string;
  if (view.phase === 'ready') text = t('countdown.ready', { seconds: seconds(view.readyInMs!) });
  else if (view.phase === 'failed')
    text = t('task.chat.countdown.failed', { reason: error(view.reason ?? 'unavailable').title });
  else text = t('task.chat.countdown.starting');
  return (
    <p className="text-center text-xs text-gray-600" data-testid="chat.countdown.summary" data-phase={view.phase}>
      {text}
    </p>
  );
}

export const chatItem: ChatItem = { kind: 'countdown', Component: Countdown };
