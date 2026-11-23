// Tab "Children" of the session detail (06-web.md sections 5.3 and 5.5, ADR-017): the subagent sessions below this
// session as a tree (depth ≤ 2 of the task's session tree), each with its label ("Subagent 1.2"), state, device, model
// and the purpose the parent requested (`subagent.request` parameters of the child's `origin_operation_id`,
// decrypted in the browser; unverifiable ones are shown with their code). Every node opens the child's own detail
// with its terminal and chat.
import { useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { OPERATION_KINDS } from '../../../../core/operations/load';
import { purposeOf, selectOperation } from '../../../../core/operations/view';
import { type SessionNode, selectSessionIndex } from '../../../../core/sessions/tree';
import type { SessionEntry } from '../../../../core/store/slices/tasks';
import { pullOnce } from '../../../../core/tasks/pull';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { SessionFacts, SessionState } from '../../../task-tabs/sessions/SessionsTab';

/** The operation that created a child session (the row's `origin_operation_id`), or null. */
export function originOf(entry: SessionEntry): string | null {
  const row: Readonly<Record<string, unknown>> = entry.row as unknown as Readonly<Record<string, unknown>>;
  return typeof row.origin_operation_id === 'string' ? row.origin_operation_id : null;
}

function Purpose({
  wg,
  operation,
  child,
}: {
  readonly wg: string;
  readonly operation: string;
  readonly child: string;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const view = useStore(
    useMemo(() => selectOperation(wg, operation), [wg, operation]),
    app.store,
  );
  const purpose = purposeOf(view, child);
  if (purpose === null) return null;
  if (purpose.status === 'unverifiable')
    return (
      <span className="text-xs text-gray-500" data-testid="children.purpose" data-status="unverifiable">
        {t('children.purpose.unverifiable', { code: purpose.code })}
      </span>
    );
  return (
    <span className="break-words text-sm" data-testid="children.purpose" data-status="verified">
      {t('children.purpose', { purpose: purpose.text })}
    </span>
  );
}

function Child({ org, node }: { readonly org: string; readonly node: SessionNode }) {
  const { t } = useI18n();
  const row = node.entry.row;
  const origin = originOf(node.entry);
  return (
    <li
      className="flex flex-col gap-1"
      data-testid="session.child"
      data-session={row.session_id}
      data-depth={node.depth}
    >
      <Link
        className="flex min-h-11 flex-wrap items-center gap-2 rounded px-2 py-1 hover:bg-gray-50"
        to={`/o/${encodeURIComponent(org)}/s/${encodeURIComponent(row.session_id)}`}
        data-testid="session.child.link"
      >
        <span className="font-medium">{t('task.chat.subagent', { path: node.path.join('.') })}</span>
        <SessionState state={row.state} />
        <SessionFacts node={node} />
      </Link>
      {origin !== null && (
        <div className="px-2">
          <Purpose wg={row.wg_id} operation={origin} child={row.session_id} />
        </div>
      )}
      {node.children.length > 0 && (
        <ul className="ml-4 flex flex-col gap-2 border-l-2 border-gray-200 pl-3">
          {node.children.map((child) => (
            <Child key={child.entry.row.session_id} org={org} node={child} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function ChildrenTab({ entry }: { readonly entry: SessionEntry }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { org_id: org, wg_id: wg, task_id: task, session_id: session } = entry.row;
  const select = useMemo(() => selectSessionIndex(task), [task]);
  const node = useStore(select, app.store).get(session);
  useEffect(() => {
    pullOnce(app.tasks, org, wg, { kinds: OPERATION_KINDS, task }).catch(() => 0);
  }, [app, org, wg, task]);
  const children = node?.children ?? [];
  if (children.length === 0)
    return (
      <p className="text-sm text-gray-600" data-testid="session.children.empty">
        {t('children.empty')}
      </p>
    );
  return (
    <ul className="flex flex-col gap-2" aria-label={t('children.label')} data-testid="session.children">
      {children.map((child) => (
        <Child key={child.entry.row.session_id} org={org} node={child} />
      ))}
    </ul>
  );
}
