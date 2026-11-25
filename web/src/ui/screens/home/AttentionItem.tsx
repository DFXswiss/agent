// One entry of the situation view (06-web.md section 4.0): the row building block of the task lists (`TaskItem`:
// title, workgroup, state, the signed owner, last activity) extended by the attention line – the reason in plain words,
// the main agent's state (a wait for model quota names when it continues, ADR-081 section 5), how long the task is in
// its group, for "Running" the phase and the active subagents – the
// next step (`Actions.tsx`, for users who may write in the workgroup), the task's linked pull requests with "Fix
// conflicts" (`PullLine.tsx`, KOR-48), "Open task", and the agent tree on demand.
//
// A record whose task row is not known yet (a live frame of a new task) loads the row first; a task of a workgroup the
// verified log does not let the user read is never shown (the hub does not send one; this is the second check).
import { memo, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ensureTask } from '../../../core/attention/load';
import { reasonText } from '../../../core/attention/reason-text';
import { actionOf } from '../../../core/attention/view';
import { parseTime } from '../../../core/encoding/time';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import { type OrgState, WG } from '../../../core/log/state';
import type { Category } from '../../../core/store/slices/attention';
import type { Directory } from '../../../core/tasks/directory';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { durationLabel, TaskItem } from '../tasks/TaskItem';
import { Actions } from './Actions';
import { PullLine } from './PullLine';
import { SessionTree } from './SessionTree';

export interface AttentionItemProps {
  readonly org: string;
  readonly id: string;
  readonly category: Category;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly now: number;
}

function Item({ org, id, category, state, userId, directory, now }: AttentionItemProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const row = useStore((store) => store.attention.rows[id]!, app.store);
  const task = useStore((store) => store.tasks.tasks[id], app.store);
  const main = task?.main_session_id ?? null;
  const agent = useStore(
    (store) => (main === null ? null : (store.tasks.sessions[main]?.row.state ?? null)),
    app.store,
  );
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (task === undefined) void ensureTask(app.tasks, id);
  }, [app, id, task]);
  if (task === undefined)
    return (
      <li
        className="border-b border-gray-100 px-2 py-3 text-sm text-gray-600"
        data-testid="attention.item"
        data-task={id}
        data-loading="true"
      >
        {t('attention.task.loading')}
      </li>
    );
  if (!state.canRead(userId, WG, task.wg_id)) return null;
  const since = parseTime(row.since);
  const reason = reasonText(category, row, { locale, timeZone: localTimeZone(), now });
  const action = actionOf(row);
  const writer = state.canWriteWg(userId, task.wg_id);
  const assign = state.isAdmin(userId) || (state.private && writer);
  const target = `/o/${encodeURIComponent(org)}/t/${encodeURIComponent(id)}`;
  return (
    <TaskItem
      org={org}
      row={task}
      directory={directory}
      now={now}
      workgroup={directory.workgroups.get(task.wg_id) ?? t('tasks.wg.unnamed')}
      testId="attention.item"
    >
      <div className="flex flex-col gap-1 px-2 pb-2 text-sm sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-3">
        <span
          className="font-medium"
          data-testid="attention.item.reason"
          data-category={row.category}
          data-reason={row.reason}
        >
          {t(reason.key, reason.params)}
        </span>
        {agent !== null && (
          <span className="text-gray-700" data-testid="attention.item.agent" data-state={agent}>
            {t('attention.agent', { state: t(`task.state.${agent}`) })}
          </span>
        )}
        <span className="text-gray-700" data-testid="attention.item.since">
          {category === 'done'
            ? t('attention.since.done', { time: formatEventTime(since, { locale, timeZone: localTimeZone(), now }) })
            : t(`attention.since.${category}`, { duration: durationLabel(locale, now - since) })}
        </span>
        {category === 'working' && row.subagents_total > 0 && (
          <span className="text-gray-700" data-testid="attention.item.subagents" data-active={row.subagents_active}>
            {t('attention.subagents', { active: row.subagents_active, total: row.subagents_total })}
          </span>
        )}
      </div>
      <PullLine org={org} task={task} writer={writer} />
      {writer && action !== null && (
        <Actions
          key={row.seq}
          org={org}
          task={task}
          row={row}
          action={action}
          assign={assign}
          owner={task.owner_id === userId}
        />
      )}
      <div className="flex flex-wrap gap-2 px-2 pb-2">
        <Link
          className="flex min-h-11 items-center rounded border border-gray-300 px-4"
          to={target}
          data-testid="attention.open"
        >
          {t('attention.open')}
        </Link>
        <button
          type="button"
          className="min-h-11 rounded px-3 text-blue-800 underline"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          data-testid="attention.item.toggle"
        >
          {open ? t('attention.toggle.hide') : t('attention.toggle.show')}
        </button>
      </div>
      {open && <SessionTree task={id} subagents={row.subagents_total} />}
    </TaskItem>
  );
}

/** An entry renders again only when its own props change (the window of a long group stays cheap). */
export const AttentionItem = memo(Item);
