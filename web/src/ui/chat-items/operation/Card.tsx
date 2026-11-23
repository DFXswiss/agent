// The operation card (06-web.md sections 5.2 and 5.4), shared by the chat items `operation.params` /
// `operation.result` and the Operations tab: type, live state badge (from the `operation` frames), a short subject
// from the decrypted parameters, the result links and values (PR URL, commit SHA, comment URL, branch, subagent
// session), "Renamed by session" for `task.set_title`, the error code, and "Duplicate of …" linking the original.
// Unverifiable parameters or results are shown as such with their code, never as content.
import { useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { LINK_REL } from '../../../core/chat/links';
import { ensureOperations, loadOperation } from '../../../core/operations/load';
import { type Fact, type OperationView, selectOperation } from '../../../core/operations/view';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

export const KNOWN_TYPES: ReadonlySet<string> = new Set([
  'github.list_repositories',
  'github.checkout',
  'github.get_pull_request',
  'github.create_branch',
  'github.commit',
  'github.push',
  'github.create_pull_request',
  'github.comment',
  'subagent.request',
  'task.set_title',
]);

export const STATE_BADGE: Readonly<Record<string, string>> = {
  succeeded: 'bg-green-100 text-green-900',
  executing: 'bg-blue-100 text-blue-900',
  failed: 'bg-red-100 text-red-900',
  rejected: 'bg-red-100 text-red-900',
  denied: 'bg-red-100 text-red-900',
  expired: 'bg-gray-200 text-gray-800',
  duplicate: 'bg-gray-200 text-gray-800',
};

/** The label of an operation type (the raw type for types this client does not know). */
export function useTypeLabel(): (type: string) => string {
  const { t } = useI18n();
  return (type) => (KNOWN_TYPES.has(type) ? t(`operations.type.${type}`) : type);
}

export function StateBadge({ state }: { readonly state: string }) {
  const { t } = useI18n();
  return (
    <span
      className={`rounded px-2 text-xs ${STATE_BADGE[state] ?? 'bg-amber-100 text-amber-900'}`}
      data-testid="operation.state"
      data-state={state}
    >
      {t(`operations.state.${state}`)}
    </span>
  );
}

/** The operations tab of a task with one operation opened. */
export const operationLink = (org: string, task: string, operation: string): string =>
  `/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}?tab=operations&op=${encodeURIComponent(operation)}`;

function FactItem({ fact, org }: { readonly fact: Fact; readonly org: string }) {
  const { t } = useI18n();
  const label = t(`operations.fact.${fact.label}`);
  const short = fact.label === 'commit' ? fact.text.slice(0, 12) : fact.text;
  let value;
  if (fact.href !== undefined)
    value = (
      <a className="text-blue-800 underline" href={fact.href} target="_blank" rel={LINK_REL}>
        {short}
      </a>
    );
  else if (fact.session !== undefined)
    value = (
      <Link
        className="text-blue-800 underline"
        to={`/o/${encodeURIComponent(org)}/s/${encodeURIComponent(fact.session)}`}
      >
        {short.slice(0, 8)}
      </Link>
    );
  else value = <span className={fact.label === 'commit' ? 'font-mono' : ''}>{short}</span>;
  return (
    <li data-testid="operation.fact" data-label={fact.label} title={fact.text}>
      {label}: {value}
    </li>
  );
}

export function CardBody({
  view,
  org,
  task,
}: {
  readonly view: OperationView;
  readonly org: string;
  readonly task: string;
}) {
  const { t, error } = useI18n();
  const typeLabel = useTypeLabel();
  const renamed = view.type === 'task.set_title';
  return (
    <div
      className="flex flex-col gap-1"
      data-testid="operation.card"
      data-operation={view.id}
      data-type={view.type}
      data-state={view.state}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium" data-testid="operation.type">
          {typeLabel(view.type)}
        </span>
        <StateBadge state={view.state} />
        {renamed && (
          <span className="text-xs text-gray-600" data-testid="operation.renamed">
            {t('operations.renamed')}
          </span>
        )}
      </div>
      {view.subject !== null && (
        <p className="break-all text-sm" data-testid="operation.subject">
          {view.subject}
        </p>
      )}
      {view.facts.length > 0 && (
        <ul className="flex flex-col text-sm" data-testid="operation.facts">
          {view.facts.map((fact) => (
            <FactItem key={`${fact.label} ${fact.text}`} fact={fact} org={org} />
          ))}
        </ul>
      )}
      {view.duplicateOf !== null && (
        <Link
          className="text-sm text-blue-800 underline"
          to={operationLink(org, task, view.duplicateOf)}
          data-testid="operation.duplicate-of"
        >
          {t('operations.duplicate-of', { id: view.duplicateOf.slice(0, 8) })}
        </Link>
      )}
      {view.errorCode !== null && (
        <p className="text-sm text-red-800" data-testid="operation.error" data-code={view.errorCode}>
          {t('operations.error', { reason: error(view.errorCode).title })}
        </p>
      )}
      {view.params.status === 'unverifiable' && (
        <p className="text-sm text-gray-500" data-testid="operation.params.unverifiable">
          {t('operations.params.unverifiable', { code: view.params.code })}
        </p>
      )}
      {view.result.status === 'unverifiable' && (
        <p className="text-sm text-gray-500" data-testid="operation.result.unverifiable">
          {t('operations.result.unverifiable', { code: view.result.code })}
        </p>
      )}
    </div>
  );
}

/** The card of `operation` in workgroup `wg` with its live row (module comment). */
export function OperationCard({
  org,
  wg,
  task,
  operation,
}: {
  readonly org: string;
  readonly wg: string;
  readonly task: string;
  readonly operation: string;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const view = useStore(
    useMemo(() => selectOperation(wg, operation), [wg, operation]),
    app.store,
  );
  const round = useStore((state) => state.operations.round, app.store);
  // the task's list once per live round (lost frames); an operation beyond its first page by ID
  useEffect(() => {
    ensureOperations(app.tasks, task)
      .then(() => app.store.getState().operations.rows[operation] ?? loadOperation(app.tasks, operation))
      .catch(() => undefined);
  }, [app, task, operation, round]);
  return (
    <div className="flex flex-col gap-1 rounded border border-gray-200 p-3">
      <CardBody view={view} org={org} task={task} />
      <Link
        className="min-h-11 self-start py-2 text-sm text-blue-800 underline"
        to={operationLink(org, task, operation)}
        data-testid="operation.open"
      >
        {t('operations.open')}
      </Link>
    </div>
  );
}
