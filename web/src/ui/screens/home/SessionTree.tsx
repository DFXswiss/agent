// The agents of a task in the situation view (06-web.md sections 4.0 and 4.0a, ADR-076 section 4, ADR-095): loaded
// only when an entry is opened (`GET /v1/tasks/{task}`, which lists at most 100 sessions), the main agent with its
// subagents indented below the agent they report to, each with its handle (`#42`, `#42.1.2`), its state, the person
// whose instruction started it ("triggered by"), a subagent's purpose where the browser can open the sealed parameters
// of its `subagent.request`, and the GitHub identity of a subagent that acts with another one than the task; at most
// `TREE_MAX` are shown, the rest are counted ("N more").
import { useEffect, useMemo, useState } from 'react';
import { taskNumber } from '../../../core/agents/name';
import { TREE_MAX } from '../../../core/attention/view';
import { OPERATION_KINDS } from '../../../core/operations/load';
import {
  flatten,
  indentLevel,
  nodeHandle,
  ownIdentity,
  type SessionNode,
  sessionTree,
} from '../../../core/sessions/tree';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { pullOnce } from '../../../core/tasks/pull';
import { loadTask, sendErrorCode } from '../../../core/tasks/send';
import { formatNumber } from '../../../core/i18n';
import '../../../core/i18n/lazy/mainagent';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { RoleBadge } from '../../profiles/RoleBadge';
import { useAppEnv } from '../../shell/context';
import { IdentityName } from '../../settings/sections/github/identities';
import { TriggeredBy } from '../../task-tabs/people';
import { originOf, Purpose } from '../session/children/Purpose';

/** Indentation by `indentLevel` (static classes, `INDENT_LEVELS` + 1 entries). */
const INDENT = ['', 'ml-4 sm:ml-6', 'ml-8 sm:ml-12', 'ml-12 sm:ml-20', 'ml-16 sm:ml-24'];

/** Who a node is, with its handle when the task has a number. */
export function nodeLabel(
  t: (key: string, params?: Record<string, string>) => string,
  node: SessionNode,
  number: number | null,
) {
  const handle = nodeHandle(node, number);
  if (node.depth === 0) return handle === null ? t('attention.tree.main') : `${t('attention.tree.main')} ${handle}`;
  return t('attention.tree.subagent', { path: handle ?? node.path.join('.') });
}

export function SessionTree({
  row,
  subagents,
  orgName,
}: {
  readonly row: TaskRow;
  readonly subagents: number;
  readonly orgName: string | null;
}) {
  const { app } = useAppEnv();
  const { t, locale, error } = useI18n();
  const task = row.task_id;
  const [status, setStatus] = useState<{ kind: 'loading' } | { kind: 'ready' } | { kind: 'error'; code: string }>({
    kind: 'loading',
  });
  useEffect(() => {
    let live = true;
    loadTask(app.tasks, task).then(
      () => live && setStatus({ kind: 'ready' }),
      (failure: unknown) => live && setStatus({ kind: 'error', code: sendErrorCode(failure) }),
    );
    // the purposes of the subagents (the sealed parameters of their `subagent.request`)
    pullOnce(app.tasks, row.org_id, row.wg_id, { kinds: OPERATION_KINDS, task }).catch(() => 0);
    return () => {
      live = false;
    };
  }, [app, task, row.org_id, row.wg_id]);
  const sessions = useStore((state) => state.tasks.sessions, app.store);
  const nodes = useMemo(() => flatten(sessionTree(sessions, task)), [sessions, task]);
  const shown = nodes.slice(0, TREE_MAX);
  const mains = nodes.filter((node) => node.depth === 0).length;
  const more = Math.max(nodes.length, mains + subagents) - shown.length;
  const number = taskNumber(row);
  return (
    <div className="flex flex-col gap-1 px-2 pb-2" data-testid="attention.tree" data-status={status.kind}>
      {status.kind === 'loading' && nodes.length === 0 && (
        <p className="text-sm text-gray-600">{t('attention.tree.loading')}</p>
      )}
      {status.kind === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="attention.tree.error">
          {error(status.code).title}
        </p>
      )}
      {status.kind === 'ready' && nodes.length === 0 && (
        <p className="text-sm text-gray-600">{t('attention.tree.empty')}</p>
      )}
      {shown.length > 0 && (
        <ul aria-label={t('attention.tree.label')} className="flex flex-col gap-1">
          {shown.map((node) => {
            const origin = originOf(node.entry);
            const identity = ownIdentity(node, row.github_identity ?? null);
            return (
              <li
                key={node.entry.row.session_id}
                className={`flex flex-wrap items-center gap-2 text-sm ${INDENT[indentLevel(node.depth)]}`}
                data-testid="attention.tree.node"
                data-depth={node.depth}
                data-session={node.entry.row.session_id}
                data-handle={nodeHandle(node, number) ?? ''}
              >
                <span>{nodeLabel(t, node, number)}</span>
                <span
                  className="rounded bg-gray-100 px-2 text-xs text-gray-800"
                  data-testid="attention.tree.state"
                  data-state={node.entry.row.state}
                >
                  {t(`task.state.${node.entry.row.state}`)}
                </span>
                <RoleBadge session={node.entry.row.session_id} task={row.task_id} />
                <TriggeredBy org={row.org_id} user={node.entry.row.triggered_by} testId="attention.tree.triggered-by" />
                {identity !== null && (
                  <span className="text-xs text-gray-700" data-testid="attention.tree.identity">
                    {t('mainagent.tree.identity')} <IdentityName id={identity} orgName={orgName} />
                  </span>
                )}
                {node.depth > 0 && origin !== null && (
                  <Purpose wg={row.wg_id} operation={origin} child={node.entry.row.session_id} />
                )}
              </li>
            );
          })}
        </ul>
      )}
      {more > 0 && (
        <p className="text-sm text-gray-600" data-testid="attention.tree.more" data-count={more}>
          {t('attention.tree.more', { count: formatNumber(more, locale) })}
        </p>
      )}
    </div>
  );
}
