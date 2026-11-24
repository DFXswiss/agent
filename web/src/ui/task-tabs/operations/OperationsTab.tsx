// Tab "Operations" of a task (06-web.md section 5.4, 03-api.md section 4.7): the task's operations newest first
// (`GET /v1/tasks/{task}/operations`, older pages on demand, new ones live from `operation` frames), each as an
// operation card; "Details" (`?tab=operations&op=<id>`) opens a drawer with the transitions (state, error code,
// time, device or hub), the decrypted parameters and the decrypted result (`GET /v1/operations/{id}` and the
// operation's events). Times are absolute local times (section 12.4). An operation with a GitHub identity or a
// triggering human (ADR-078 section 6) says "as <login>, triggered by <name>": the login from the identities the
// browser can open (a company account by its organization, else "another account"), the name from the members'
// profiles; both are rendered as text only.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { formatEventTime } from '../../../core/i18n';
import {
  OPERATION_KINDS,
  ensureOperations,
  loadOperation,
  loadOperations,
  pullOperationEvents,
} from '../../../core/operations/load';
import { type Part, selectOperation } from '../../../core/operations/view';
import { newestFirst, type OperationRow } from '../../../core/store/slices/operations';
import { memoize } from '../../../core/store/selectors';
import { pullOnce } from '../../../core/tasks/pull';
import { CardBody, operationLink, StateBadge, useTypeLabel } from '../../chat-items/operation/Card';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { Dialog } from '../../screens/members/Dialog';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useDeviceNames } from '../../screens/devices/labels';
import { useDirectory } from '../../screens/tasks/hooks';
import { IdentityName, useIdentities } from '../../settings/sections/github/identities';

/** "as <identity>, triggered by <name>" of an operation (module comment); nothing for an operation without either. */
function Attribution({ org, row }: { readonly org: string; readonly row: OperationRow }) {
  const { t } = useI18n();
  const directory = useDirectory(org, false);
  useIdentities();
  const identity = row.github_identity ?? null;
  const by = row.triggered_by ?? null;
  if (identity === null && by === null) return null;
  const name = by === null ? null : (directory.members.get(by) ?? t('identities.member.unknown'));
  return (
    <p className="text-xs text-gray-600" data-testid="operations.attribution">
      {identity !== null && (
        <>
          {t('identities.as')} <IdentityName id={identity} orgName={directory.orgName} />
        </>
      )}
      {identity !== null && name !== null && ', '}
      {name !== null && <span data-testid="operations.triggered-by">{t('identities.triggered-by', { name })}</span>}
    </p>
  );
}

const rowsOf = memoize((rows: Readonly<Record<string, OperationRow>>) => Object.values(rows));

function useTime(): (at: string) => string {
  const { locale } = useI18n();
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (at) => {
    const ms = Date.parse(at);
    return Number.isNaN(ms) ? at : formatEventTime(ms, { locale, timeZone, now: Date.now() });
  };
}

function Json<T>({
  part,
  testId,
  pick,
  unverifiable,
}: {
  readonly part: Part<T>;
  readonly testId: string;
  readonly pick: (value: T) => unknown;
  /** Message key of the unverifiable text. */
  readonly unverifiable: string;
}) {
  const { t } = useI18n();
  if (part.status === 'verified')
    return (
      <pre
        className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-50 p-2 text-xs"
        data-testid={testId}
      >
        {JSON.stringify(pick(part.value), null, 2)}
      </pre>
    );
  return (
    <p className="text-sm text-gray-500" data-testid={testId} data-status={part.status}>
      {part.status === 'unverifiable' ? t(unverifiable, { code: part.code }) : t('operations.detail.none')}
    </p>
  );
}

function Detail({
  org,
  row,
  onClose,
}: {
  readonly org: string;
  readonly row: OperationRow;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const time = useTime();
  const typeLabel = useTypeLabel();
  const deviceName = useDeviceNames([org]);
  const view = useStore(
    useMemo(() => selectOperation(row.wg_id, row.operation_id), [row.wg_id, row.operation_id]),
    app.store,
  );
  const transitions = useStore((state) => state.operations.transitions[row.operation_id] ?? null, app.store);
  const [failed, setFailed] = useState<string | null>(null);
  // the detail loads once per opened operation, not again for every newer row of it
  const first = useRef(row);
  useEffect(() => {
    let live = true;
    Promise.all([
      loadOperation(app.tasks, first.current.operation_id),
      pullOperationEvents(app.tasks, first.current),
    ]).catch((caught: unknown) => live && setFailed(errorCode(caught)));
    return () => {
      live = false;
    };
  }, [app]);
  return (
    <Dialog
      title={t('operations.detail.title', { type: typeLabel(view.type) })}
      testId="operations.detail"
      onClose={onClose}
    >
      <CardBody view={view} org={org} task={row.task_id} />
      {failed !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="operations.detail.failed">
          {t('operations.detail.failed', { reason: error(failed).title })}
        </p>
      )}
      <h3 className="font-semibold">{t('operations.detail.transitions')}</h3>
      {transitions === null ? (
        <p className="text-sm" data-testid="operations.detail.loading">
          {t('operations.detail.loading')}
        </p>
      ) : (
        <ol className="flex flex-col gap-1 text-sm" data-testid="operations.transitions">
          {transitions.map((step) => (
            <li
              key={step.seq}
              className="flex flex-wrap items-center gap-2"
              data-testid="operations.transition"
              data-state={step.state}
            >
              <StateBadge state={step.state} />
              <time dateTime={step.at}>{time(step.at)}</time>
              <span className="text-gray-600">
                {step.by_id === null
                  ? t('operations.detail.by-hub')
                  : t('operations.detail.by-device', { device: deviceName(step.by_id) })}
              </span>
              {step.error_code !== null && <span className="text-red-800">{error(step.error_code).title}</span>}
            </li>
          ))}
        </ol>
      )}
      {row.retryable && <p className="text-sm">{t('operations.detail.retryable')}</p>}
      <h3 className="font-semibold">{t('operations.detail.params')}</h3>
      <Json
        part={view.params}
        testId="operations.detail.params"
        pick={(value) => value.params}
        unverifiable="operations.params.unverifiable"
      />
      <h3 className="font-semibold">{t('operations.detail.result')}</h3>
      <Json
        part={view.result}
        testId="operations.detail.result"
        unverifiable="operations.result.unverifiable"
        pick={(value) => (value.result !== null ? value.result : value.error)}
      />
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={onClose}
        data-testid="operations.detail.close"
      >
        {t('operations.detail.close')}
      </button>
    </Dialog>
  );
}

function Item({ org, row }: { readonly org: string; readonly row: OperationRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const time = useTime();
  const view = useStore(
    useMemo(() => selectOperation(row.wg_id, row.operation_id), [row.wg_id, row.operation_id]),
    app.store,
  );
  return (
    <li
      className="flex flex-col gap-1 rounded border border-gray-200 p-3"
      data-testid="operations.item"
      data-operation={row.operation_id}
    >
      <time className="text-xs text-gray-500" dateTime={row.created_at}>
        {time(row.created_at)}
      </time>
      <CardBody view={view} org={org} task={row.task_id} />
      <Attribution org={org} row={row} />
      <Link
        className="min-h-11 self-start py-2 text-sm text-blue-800 underline"
        to={operationLink(org, row.task_id, row.operation_id)}
        data-testid="operations.open"
      >
        {t('operations.open')}
      </Link>
    </li>
  );
}

export function OperationsTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const all = useStore((state) => rowsOf(state.operations.rows), app.store);
  const page = useStore((state) => state.operations.tasks[taskId] ?? null, app.store);
  // the tab is shown for a loaded task only (`TaskView`)
  const wg = useStore((state) => state.tasks.tasks[taskId]!.wg_id, app.store);
  const round = useStore((state) => state.operations.round, app.store);
  const [failed, setFailed] = useState<string | null>(null);
  const rows = useMemo(() => all.filter((row) => row.task_id === taskId).sort(newestFirst), [all, taskId]);
  const opened = new URLSearchParams(location.search).get('op');
  // the original of a duplicate may belong to another task of the workgroup (dedupe slots are per workgroup)
  const openedRow = all.find((row) => row.operation_id === opened) ?? null;
  const known = openedRow !== null;
  useEffect(() => {
    ensureOperations(app.tasks, taskId).then(
      () => setFailed(null),
      (caught: unknown) => setFailed(errorCode(caught)),
    );
  }, [app, taskId, round]);
  // an operation opened by link that is not in the loaded pages (an older one, a duplicate's original)
  useEffect(() => {
    if (opened !== null && !known) loadOperation(app.tasks, opened).catch(() => undefined);
  }, [app, opened, known]);
  useEffect(() => {
    pullOnce(app.tasks, orgId, wg, { kinds: OPERATION_KINDS, task: taskId }).catch(() => 0);
  }, [app, orgId, wg, taskId]);
  const more = () => {
    setFailed(null);
    loadOperations(app.tasks, taskId, page!.nextBefore).catch((caught: unknown) => setFailed(errorCode(caught)));
  };
  const close = () => navigate({ pathname: location.pathname, search: '?tab=operations' });
  return (
    <section aria-label={t('operations.label')} className="flex flex-col gap-3" data-testid="operations">
      {failed !== null && (
        <p role="alert" className="text-red-800" data-testid="operations.failed">
          {t('operations.failed', { reason: error(failed).title })}
        </p>
      )}
      {page === null && failed === null && <p data-testid="operations.loading">{t('operations.loading')}</p>}
      {page !== null && rows.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="operations.empty">
          {t('operations.empty')}
        </p>
      )}
      {rows.length > 0 && (
        <ol className="flex flex-col gap-3" data-testid="operations.list">
          {rows.map((row) => (
            <Item key={row.operation_id} org={orgId} row={row} />
          ))}
        </ol>
      )}
      {page !== null && page.nextBefore !== null && (
        <button type="button" className="min-h-11 self-start underline" onClick={more} data-testid="operations.more">
          {t('operations.more')}
        </button>
      )}
      {openedRow !== null && <Detail key={openedRow.operation_id} org={orgId} row={openedRow} onClose={close} />}
    </section>
  );
}
