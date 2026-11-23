// Slice `audit` (06-web.md section 9.2): one view per org and hub filter set (rows newest first, the cursor of the next
// page) and the newest chain verification per org. The time filter is not a hub parameter (core/monitoring/audit.ts):
// a load fetches further pages on its own while a page holds no row of the time range and the range can still be
// reached, at most `AUTO_PAGES` pages per load, so that a narrow time window does not need many "Load more" clicks.
import {
  type AuditFilters,
  type AuditRow,
  auditTarget,
  hubQuery,
  inTime,
  parseAuditPage,
  parseVerify,
  pastRange,
  type VerifyResult,
} from '../../monitoring/audit';
import { sendErrorCode } from '../../tasks/send';
import type { Slice, Store } from '../store';
import type { MonitoringContext } from './monitoring';

/** Pages fetched by one load at most while none of them holds a row of the time range. */
export const AUTO_PAGES = 5;

export interface AuditView {
  readonly status: 'loading' | 'ready' | 'error';
  readonly code: string | null;
  readonly rows: readonly AuditRow[];
  /** The cursor of the next page, null at the end. */
  readonly next: string | null;
}

export interface VerifyState {
  readonly status: 'loading' | 'ready' | 'error';
  readonly code: string | null;
  readonly result: VerifyResult | null;
  /** When the result arrived (client clock). */
  readonly at: number | null;
}

export interface AuditState {
  readonly views: Readonly<Record<string, AuditView>>;
  readonly verify: Readonly<Record<string, VerifyState>>;
}

declare module '../store' {
  interface StoreState {
    audit: AuditState;
  }
}

export const slice: Slice<'audit'> = { name: 'audit', initial: () => ({ views: {}, verify: {} }) };

/** The store key of the view of `org` with the hub filters of `filters`. */
export const viewKey = (org: string, filters: AuditFilters): string => `${org}?${hubQuery(filters)}`;

const EMPTY: AuditView = { status: 'loading', code: null, rows: [], next: null };

export const auditView = (state: AuditState, key: string): AuditView => state.views[key] ?? EMPTY;

/** The rows of a view inside the time filter. */
export const visibleRows = (view: AuditView, filters: AuditFilters): AuditRow[] =>
  view.rows.filter((row) => inTime(row, filters));

/** Whether "Load more" can find further rows of the time range. */
export const canLoadMore = (view: AuditView, filters: AuditFilters): boolean =>
  view.next !== null && !pastRange(view.rows, filters);

function put(store: Store, key: string, view: AuditView): void {
  store.update('audit', (state) => ({ ...state, views: { ...state.views, [key]: view } }));
}

/**
 * Loads the first page of a view (`more` false: the view starts again) or the next one (`more` true), then further
 * pages while none of them showed a row of the time range (at most `AUTO_PAGES` per call).
 */
export async function loadAudit(
  context: MonitoringContext,
  org: string,
  filters: AuditFilters,
  more = false,
): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const key = viewKey(org, filters);
  const start = more ? auditView(store.getState().audit, key) : EMPTY;
  if (more && !canLoadMore(start, filters)) return;
  let rows = start.rows;
  let next = start.next;
  put(store, key, { ...start, status: 'loading', code: null });
  try {
    for (let page = 0; page < AUTO_PAGES; page += 1) {
      const found = parseAuditPage(await context.client.request('GET', auditTarget(org, filters, next)));
      rows = [...rows, ...found.rows];
      next = found.next;
      const view = { status: 'loading' as const, code: null, rows, next };
      if (found.rows.some((row) => inTime(row, filters)) || !canLoadMore(view, filters)) break;
    }
    if (store.generation === generation) put(store, key, { status: 'ready', code: null, rows, next });
  } catch (error) {
    if (store.generation === generation) put(store, key, { status: 'error', code: sendErrorCode(error), rows, next });
  }
}

const NO_VERIFY: VerifyState = { status: 'ready', code: null, result: null, at: null };

export const verifyState = (state: AuditState, org: string): VerifyState => state.verify[org] ?? NO_VERIFY;

/** Verifies the audit chain (`GET /v1/orgs/{org}/audit/verify`, org admins). */
export async function verifyAudit(context: MonitoringContext, org: string): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const set = (entry: VerifyState) =>
    store.update('audit', (state) => ({ ...state, verify: { ...state.verify, [org]: entry } }));
  set({ status: 'loading', code: null, result: null, at: null });
  try {
    const result = parseVerify(await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/audit/verify`));
    if (store.generation === generation) set({ status: 'ready', code: null, result, at: context.now() });
  } catch (error) {
    if (store.generation === generation) set({ status: 'error', code: sendErrorCode(error), result: null, at: null });
  }
}
