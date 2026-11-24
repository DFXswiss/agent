// The audit rows of one task (06-web.md section 9.2): the audit view with the task filter fixed.
import type { TaskTabProps } from '../../registries/task-tabs';
import { AuditView } from '../../screens/audit/AuditView';

export function AuditTab({ orgId, taskId }: TaskTabProps) {
  return <AuditView org={orgId} task={taskId} listId="task.audit" />;
}
