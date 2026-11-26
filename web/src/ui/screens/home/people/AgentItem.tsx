// One main agent of a person (06-web.md section 4.0a, ADR-095 sections 5 and 6): the row of the task lists (`TaskItem`:
// title with the agent's name and handle, workgroup, state, owner, last activity) and below it the attention reason, the
// GitHub identity the agent acts as ("Automatic" without a choice), its active and total subagents, "Rename" for
// writers of the workgroup, "Open task" and the agent tree on demand.
import { memo, useState } from 'react';
import { Link } from 'react-router-dom';
import { selectAgentAttention } from '../../../../core/agents/people';
import { reasonText } from '../../../../core/attention/reason-text';
import { localTimeZone } from '../../../../core/i18n';
import { type OrgState, WG } from '../../../../core/log/state';
import type { Category } from '../../../../core/store/slices/attention';
import type { Directory } from '../../../../core/tasks/directory';
import { RenameAgent } from '../../../agents/AgentName';
import { RoleBadge } from '../../../profiles/RoleBadge';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { IdentityName } from '../../../settings/sections/github/identities';
import { useAppEnv } from '../../../shell/context';
import { TaskEntry } from '../../tasks/TaskItem';
import { SessionTree } from '../SessionTree';

const IDS = { root: 'agents.item.agent', name: 'agents.item.name', handle: 'agents.item.handle' };

export interface AgentItemProps {
  readonly org: string;
  readonly id: string;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly now: number;
}

function Item({ org, id, state, userId, directory, now }: AgentItemProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const task = useStore((store) => store.tasks.tasks[id]!, app.store);
  const attention = useStore((store) => selectAgentAttention(id)(store), app.store);
  const [open, setOpen] = useState(false);
  // a task of a workgroup the verified log does not let the user read is never shown (the hub does not send one)
  if (!state.canRead(userId, WG, task.wg_id)) return null;
  const writer = state.canWriteWg(userId, task.wg_id);
  // a record's category is one of the four (the slices accept no other)
  const reason =
    attention === null
      ? null
      : reasonText(attention.category as Category, attention, { locale, timeZone: localTimeZone(), now });
  const identity = task.github_identity ?? null;
  return (
    <TaskEntry
      org={org}
      row={task}
      directory={directory}
      now={now}
      workgroup={directory.workgroups.get(task.wg_id) ?? t('tasks.wg.unnamed')}
      testId="agents.item"
      agentIds={IDS}
    >
      <div className="flex flex-col gap-1 px-2 pb-2 text-sm sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-3">
        <span
          className="font-medium"
          data-testid="agents.item.reason"
          data-category={attention?.category ?? ''}
          data-reason={attention?.reason ?? ''}
        >
          {reason === null ? t('mainagent.item.no-attention') : t(reason.key, reason.params)}
        </span>
        {/* the role of the main agent is the rights profile of its task (ADR-094 section 1): the badge falls back to
            the task's profile for an ID that names no session row */}
        <RoleBadge session={id} task={id} />
        <span className="text-gray-700" data-testid="agents.item.identity" data-identity={identity ?? ''}>
          {'GitHub: '}
          {identity === null ? (
            t('mainagent.item.identity.automatic')
          ) : (
            <IdentityName id={identity} orgName={directory.orgName} />
          )}
        </span>
        {attention !== null && (
          <span
            className="text-gray-700"
            data-testid="agents.item.subagents"
            data-active={attention.subagents_active}
            data-total={attention.subagents_total}
          >
            {t('attention.subagents', { active: attention.subagents_active, total: attention.subagents_total })}
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-2 px-2 pb-2">
        <Link
          className="flex min-h-11 items-center rounded border border-gray-300 px-4"
          to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(id)}`}
          data-testid="agents.item.open"
        >
          {t('attention.open')}
        </Link>
        <button
          type="button"
          className="min-h-11 rounded px-3 text-blue-800 underline"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          data-testid="agents.item.toggle"
        >
          {open ? t('attention.toggle.hide') : t('attention.toggle.show')}
        </button>
        {writer && <RenameAgent row={task} prefix="agents.item.rename" />}
      </div>
      {open && <SessionTree row={task} subagents={attention?.subagents_total ?? 0} orgName={directory.orgName} />}
    </TaskEntry>
  );
}

/** An agent renders again only when its own props change (the window of a long list stays cheap). */
export const AgentItem = memo(Item);
