// Hooks of the pull-request rows (KOR-48): the status of a linked pull request – read lazily by the device request
// once a row is shown (`core/pulls/status.ts` batches the rows of a view into one request) – the fix feedback from the
// task's operation events (pulled once per task while the pull request is open) with the fresh read after it, and the
// title of a linked task (its row loaded once when the browser does not hold it).
import { useEffect, useMemo } from 'react';
import { ensureTask } from '../../core/attention/load';
import { OPERATION_KINDS } from '../../core/operations/load';
import { type PullRef, pullKey } from '../../core/pulls/address';
import { type Fixed, selectFixed } from '../../core/pulls/fixed';
import { checkPulls, refreshAfterFix, type StatusTarget } from '../../core/pulls/status';
import { displayOf, type PullDisplay } from '../../core/pulls/view';
import { lastState, pullStatus, type PullStatus } from '../../core/pulls/state';
import { selectTitle, type Title } from '../../core/tasks/directory';
import { pullOnce } from '../../core/tasks/pull';
import { useStore } from '../hooks/useStore';
import { useAppEnv } from '../shell/context';

export interface PullView {
  readonly status: PullStatus | null;
  readonly display: PullDisplay;
  readonly fixed: Fixed | null;
}

const MISSING: Title = { status: 'missing' };

/** The status, display and fix of a pull request linked to `target.task` (module comment); null: nothing is read. */
export function usePull(target: StatusTarget & { readonly task: string }, pull: PullRef | null): PullView {
  const { app } = useAppEnv();
  const { org, wg, task } = target;
  const repo = pull?.repo ?? null;
  const number = pull?.number ?? 0;
  // one object per pull request, whatever object the caller passes
  const ref = useMemo(() => (repo === null ? null : { repo, number }), [repo, number]);
  const status = useStore((state) => (ref === null ? null : pullStatus(state.pulls, wg, pullKey(ref))), app.store);
  const display = ref === null ? ({ kind: 'unchecked' } as const) : displayOf(ref, status);
  // the head branch of the newest checked state: the fix feedback stays while the state is read again
  const last = lastState(status);
  const headRef = last?.found === true ? last.head_ref : null;
  const live = display.kind === 'found' && display.conflict !== null;
  const select = useMemo(
    () => (ref === null ? () => null : selectFixed(wg, task, ref, headRef)),
    [wg, task, ref, headRef],
  );
  const fixed = useStore(select, app.store);
  const commit = fixed?.commit ?? null;
  useEffect(() => {
    if (ref !== null && status === null) checkPulls(app.tasks, { org, wg, task }, [ref]);
  }, [app, org, wg, task, ref, status]);
  useEffect(() => {
    if (live) pullOnce(app.tasks, org, wg, { kinds: OPERATION_KINDS, task }).catch(() => 0);
  }, [app, org, wg, task, live]);
  useEffect(() => {
    if (ref !== null && commit !== null) refreshAfterFix(app.tasks, { org, wg, task }, ref, commit);
  }, [app, org, wg, task, ref, commit]);
  return { status, display, fixed };
}

/** The title of a task the row links to (module comment). */
export function useTaskTitle(task: string): Title {
  const { app } = useAppEnv();
  const row = useStore((state) => state.tasks.tasks[task], app.store);
  const select = useMemo(
    () => (row === undefined ? () => MISSING : selectTitle(row.wg_id, row.task_id, row.title_event_id)),
    [row],
  );
  const title = useStore(select, app.store);
  useEffect(() => {
    if (row === undefined) void ensureTask(app.tasks, task);
  }, [app, task, row]);
  useEffect(() => {
    if (row !== undefined && title.status === 'missing')
      pullOnce(app.tasks, row.org_id, row.wg_id, { kinds: ['task.meta'], task }).catch(() => 0);
  }, [app, task, row, title.status]);
  return title;
}
