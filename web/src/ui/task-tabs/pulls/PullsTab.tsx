// Tab "Pull requests" of a task (KOR-48, ADR-084 sections 1, 3 and 5): the pull requests linked to this task – the
// ones its sessions created and the ones a person linked by address – with their state and conflict state, "Fix
// conflicts" (writers), "Conflicts fixed, branch updated" with the merge commit after a fix, and for writers "Paste a
// pull-request address" that links to this task. Shown once the task's row is known (the task page loads it).
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { LinkForm } from '../../pulls/LinkForm';
import { LinkList } from '../../pulls/LinkList';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useOrgView } from '../../screens/tasks/hooks';
import { useAppEnv } from '../../shell/context';

export function PullsTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const row = useStore((state) => state.tasks.tasks[taskId], app.store);
  const { state, userId } = useOrgView(orgId);
  if (row === undefined || state === null)
    return (
      <p className="text-sm text-gray-600" data-testid="task.pulls.loading">
        {t('pulls.loading')}
      </p>
    );
  const writer = userId !== null && state.canWriteWg(userId, row.wg_id);
  return (
    <section className="flex flex-col gap-4" aria-label={t('pulls.tab')} data-testid="task.pulls">
      <LinkList org={orgId} wg={row.wg_id} task={taskId} writer={writer} />
      {writer && <LinkForm org={orgId} wg={row.wg_id} task={taskId} />}
    </section>
  );
}
