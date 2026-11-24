// Tab "Operations" of a task (06-web.md section 5.4, 03-api.md section 4.7): the task's operations newest first
// (`GET /v1/tasks/{task}/operations`, older pages on demand, new ones live from `operation` frames), each as an
// operation card; "Details" (`?tab=operations&op=<id>`) opens a drawer with the transitions (state, error code,
// time, device or hub), the decrypted parameters and the decrypted result (`GET /v1/operations/{id}` and the
// operation's events). Times are absolute local times (section 12.4).
//
// Many operations (ADR-063 section 1): the rows are a `WindowedList` (newest first, `WINDOW` rows at first, `PAGE` per
// move, at most `MAX_ROWS`), older pages from the hub by "Load more"; one selector builds the views of the task's
// operations (memoized per row and per operation's events), so a change renders only the changed rows. The detail's
// JSON is pretty-printed and clipped (`Clipped`, "Show more" and "Download"), built only while the detail is open.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import { prettyJson } from '../../../core/lists/text';
import {
  OPERATION_KINDS,
  ensureOperations,
  loadOperation,
  loadOperations,
  pullOperationEvents,
} from '../../../core/operations/load';
import {
  type OperationView,
  operationView,
  type ParamsBody,
  type Part,
  type ResultBody,
  selectOperation,
} from '../../../core/operations/view';
import { isRecord } from '../../../core/registry';
import type { EventEntry, ScopeEvents } from '../../../core/store/slices/events';
import { newestFirst, type OperationRow } from '../../../core/store/slices/operations';
import { memoize } from '../../../core/store/selectors';
import type { StoreState } from '../../../core/store/store';
import { pullOnce } from '../../../core/tasks/pull';
import { CardBody, operationLink, StateBadge, useTypeLabel } from '../../chat-items/operation/Card';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { Clipped } from '../../lists/Clipped';
import { WindowedList } from '../../lists/WindowedList';
import { Dialog } from '../../screens/members/Dialog';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useDeviceNames } from '../../screens/devices/labels';

/** Rows rendered at first, rows added by a move, the cap. */
export const WINDOW = 100;
export const PAGE = 50;
export const MAX_ROWS = 200;

const KINDS: ReadonlySet<unknown> = new Set(OPERATION_KINDS);
const NO_EVENTS: readonly EventEntry[] = [];

/** The header an entry claims, as `operationView` reads it: the verified one, else the unchecked one. */
function claimed(entry: EventEntry): Readonly<Record<string, unknown>> {
  if (entry.status === 'verified') return entry.header!;
  return isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};
}

/** The operation events of a scope by their operation (`ref`), built once per scope state. */
const eventsByOperation = memoize((scope: ScopeEvents): ReadonlyMap<string, readonly EventEntry[]> => {
  const index = new Map<string, EventEntry[]>();
  for (const entry of scope.entries.values()) {
    if (entry.status === 'pending' || entry.status === 'locked') continue;
    const header = claimed(entry);
    if (!KINDS.has(header.kind) || typeof header.ref !== 'string') continue;
    const list = index.get(header.ref);
    if (list === undefined) index.set(header.ref, [entry]);
    else list.push(entry);
  }
  return index;
});

const sameList = <T,>(a: readonly T[], b: readonly T[]): boolean =>
  a.length === b.length && a.every((item, i) => item === b[i]);

interface Built {
  readonly row: OperationRow;
  readonly events: readonly EventEntry[];
  readonly view: OperationView;
}

/**
 * The views of the operations of `task` in workgroup `wg`, newest first: the rows are filtered once per change of the
 * rows, a view is built again only when its row or its events changed, and the same array is returned while no view
 * changed (other tasks' operations do not re-render the tab).
 */
export function selectTaskOperations(wg: string, task: string): (state: StoreState) => readonly OperationView[] {
  const ownRows = memoize((rows: Readonly<Record<string, OperationRow>>) =>
    Object.values(rows)
      .filter((row) => row.task_id === task)
      .sort(newestFirst),
  );
  let built = new Map<string, Built>();
  let last: readonly OperationView[] = [];
  return (state) => {
    const scope = state.events[wg];
    const index = scope === undefined ? null : eventsByOperation(scope);
    const next = new Map<string, Built>();
    const views = ownRows(state.operations.rows).map((row) => {
      const events = index?.get(row.operation_id) ?? NO_EVENTS;
      const known = built.get(row.operation_id);
      const kept =
        known !== undefined && known.row === row && sameList(known.events, events)
          ? known
          : { row, events, view: operationView(events, row.operation_id, row) };
      next.set(row.operation_id, kept);
      return kept.view;
    });
    built = next;
    if (!sameList(views, last)) last = views;
    return last;
  };
}

/** The local time of `at` (the time zone is read once per page load). */
function useTime(): (at: string) => string {
  const { locale } = useI18n();
  return (at) => {
    const ms = Date.parse(at);
    return Number.isNaN(ms) ? at : formatEventTime(ms, { locale, timeZone: localTimeZone(), now: Date.now() });
  };
}

function Json<T>({
  part,
  testId,
  pick,
  file,
  unverifiable,
}: {
  readonly part: Part<T>;
  readonly testId: string;
  readonly pick: (value: T) => unknown;
  /** File name of "Download". */
  readonly file: string;
  /** Message key of the unverifiable text. */
  readonly unverifiable: string;
}) {
  const { t } = useI18n();
  if (part.status === 'verified') return <VerifiedJson value={pick(part.value)} testId={testId} file={file} />;
  return (
    <p className="text-sm text-gray-500" data-testid={testId} data-status={part.status}>
      {part.status === 'unverifiable' ? t(unverifiable, { code: part.code }) : t('operations.detail.none')}
    </p>
  );
}

/** A decrypted value as indented JSON, clipped (a result may hold up to 1 MiB of text). */
function VerifiedJson({
  value,
  testId,
  file,
}: {
  readonly value: unknown;
  readonly testId: string;
  readonly file: string;
}) {
  const text = useMemo(() => prettyJson(value), [value]);
  return (
    <Clipped
      text={text}
      className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-50 p-2 text-xs"
      testId={testId}
      controls={testId}
      file={file}
    />
  );
}

const pickParams = (value: ParamsBody): unknown => value.params;
const pickResult = (value: ResultBody): unknown => (value.result !== null ? value.result : value.error);

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
        pick={pickParams}
        file="operation-params.json"
        unverifiable="operations.params.unverifiable"
      />
      <h3 className="font-semibold">{t('operations.detail.result')}</h3>
      <Json
        part={view.result}
        testId="operations.detail.result"
        unverifiable="operations.result.unverifiable"
        file="operation-result.json"
        pick={pickResult}
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

function Item({ item: view }: { readonly item: OperationView }) {
  const { t } = useI18n();
  const time = useTime();
  // the views of the list come from the task's rows: each has its row
  const row = view.row!;
  return (
    <div className="flex flex-col gap-1">
      <time className="text-xs text-gray-500" dateTime={row.created_at}>
        {time(row.created_at)}
      </time>
      <CardBody view={view} org={row.org_id} task={row.task_id} />
      <Link
        className="min-h-11 self-start py-2 text-sm text-blue-800 underline"
        to={operationLink(row.org_id, row.task_id, row.operation_id)}
        data-testid="operations.open"
      >
        {t('operations.open')}
      </Link>
    </div>
  );
}

const key = (view: OperationView): string => view.id;
const attributes = (view: OperationView) => ({ 'data-testid': 'operations.item', 'data-operation': view.id });

export function OperationsTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const page = useStore((state) => state.operations.tasks[taskId] ?? null, app.store);
  // the tab is shown for a loaded task only (`TaskView`)
  const wg = useStore((state) => state.tasks.tasks[taskId]!.wg_id, app.store);
  const round = useStore((state) => state.operations.round, app.store);
  const views = useStore(
    useMemo(() => selectTaskOperations(wg, taskId), [wg, taskId]),
    app.store,
  );
  const [failed, setFailed] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const opened = new URLSearchParams(location.search).get('op');
  // the original of a duplicate may belong to another task of the workgroup (dedupe slots are per workgroup)
  const openedRow = useStore((state) => (opened === null ? null : (state.operations.rows[opened] ?? null)), app.store);
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
    setLoading(true);
    loadOperations(app.tasks, taskId, page!.nextBefore)
      .catch((caught: unknown) => setFailed(errorCode(caught)))
      .finally(() => setLoading(false));
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
      <WindowedList
        listId="task.operations"
        items={views}
        getKey={key}
        Row={Item}
        newest="start"
        size={WINDOW}
        page={PAGE}
        maxRows={MAX_ROWS}
        as="ol"
        className="flex flex-col gap-3"
        rowClassName="rounded border border-gray-200 p-3"
        rowHeight="lg"
        rowAttributes={attributes}
        testId="operations.list"
        testIds={{ loadMore: 'operations.more' }}
        hasMore={page !== null && page.nextBefore !== null}
        loading={loading}
        onLoadMore={more}
        empty={
          page !== null && (
            <p className="text-sm text-gray-600" data-testid="operations.empty">
              {t('operations.empty')}
            </p>
          )
        }
      />
      {openedRow !== null && <Detail key={openedRow.operation_id} org={orgId} row={openedRow} onClose={close} />}
    </section>
  );
}
