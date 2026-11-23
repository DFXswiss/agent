// The whiteboard of a task (06-web.md section 6.1): the board of the task's workgroup scope with the task set; read
// with `task:read`, written with `task:write` (= `wg:write` of the task's workgroup). The task page renders tabs only
// once the task row is in the store; without it (e.g. right after a sign-out) the tab shows nothing.
import { useMemo } from 'react';
import { taskBoard } from '../../../core/whiteboards/location';
import { useStore } from '../../hooks/useStore';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useAppEnv } from '../../shell/context';
import { Board } from '../../whiteboard/Board';

export function WhiteboardTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const wg = useStore((state) => state.tasks.tasks[taskId]?.wg_id ?? null, app.store);
  const location = useMemo(() => (wg === null ? null : taskBoard(orgId, wg, taskId)), [orgId, wg, taskId]);
  return location === null ? null : <Board location={location} />;
}
