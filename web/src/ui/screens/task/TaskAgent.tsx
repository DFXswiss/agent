// The main agent of the task in the task page header (06-web.md section 4.0a, ADR-095): its name and handle, and for
// writers of the workgroup "Rename" (a field with the current name, "Save", "Automatic name"). An automatic name needs
// the title: the header pulls the task's own `task.meta` events (never the whole workgroup's, which can be thousands)
// when no list has brought them (the task page opened directly).
import { useEffect } from 'react';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { TITLE_KIND } from '../../../core/tasks/directory';
import { pullOnce } from '../../../core/tasks/pull';
import { AgentName, RenameAgent } from '../../agents/AgentName';
import { useAppEnv } from '../../shell/context';
import { useTitle } from '../tasks/hooks';

const IDS = { root: 'task.agent', name: 'task.agent.name', handle: 'task.agent.handle' };

export function TaskAgent({ row, writer }: { readonly row: TaskRow; readonly writer: boolean }) {
  const { app } = useAppEnv();
  const missing = useTitle(row).status === 'missing';
  const { org_id: org, wg_id: wg, task_id: task } = row;
  useEffect(() => {
    if (missing) pullOnce(app.tasks, org, wg, { kinds: [TITLE_KIND], task }).catch(() => 0);
  }, [app, org, wg, task, missing]);
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <AgentName row={row} ids={IDS} className="font-medium" />
      {writer && <RenameAgent row={row} prefix="task.agent.rename" />}
    </div>
  );
}
