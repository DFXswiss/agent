// The card that starts the missing model access (rule E-05, ADR-079 section 4): when the task's main session failed
// with `model_access_unavailable`, its owner sees in the chat a card that leads to the connection of the Claude
// subscription (settings, 06-web.md section 8.2). The hub continues the task by itself once a model credential is
// stored; nobody repeats the instruction. Others see that the owner has been asked.
import { Link } from 'react-router-dom';
import type { SessionEntry, TaskRow } from '../../../core/store/slices/tasks';
import { useI18n } from '../../i18n';

export const MODEL_ACCESS_REASON = 'model_access_unavailable';

export function ModelAccessCard({
  task,
  main,
  userId,
}: {
  readonly task: TaskRow;
  readonly main: SessionEntry | null;
  readonly userId: string | null;
}) {
  const { t } = useI18n();
  if (main?.row.state !== 'failed' || main.row.state_reason !== MODEL_ACCESS_REASON) return null;
  const owner = userId === task.owner_id;
  return (
    <div
      className="rounded border border-amber-300 bg-amber-50 p-3 text-sm"
      role="status"
      data-testid="chat.model-access"
    >
      <p>{t(owner ? 'task.chat.model-access.owner' : 'task.chat.model-access.other')}</p>
      {owner && (
        <Link
          className="mt-2 inline-flex min-h-11 items-center underline"
          to="/settings"
          data-testid="chat.model-access.connect"
        >
          {t('task.chat.model-access.connect')}
        </Link>
      )}
    </div>
  );
}
