// Slice `monitoring` (06-web.md section 9.1, 08-monitoring.md section 6): the overview per org, the series of the open
// charts and the VM of each session (from its session row, read once – the overview's session items carry none).
// Live: the `metrics` frames of the active org's channel (only while a monitoring view is open, `monitoring=1`) replace
// the newest sample of the entities they name if they are newer; a sample of an entity the overview does not list marks
// the overview for a reload (a new VM or session), which the screen does at most every `RELOAD_MS` – an entity that a
// reload still does not list (the hub samples more than the overview shows, e.g. a deleted VM) is not asked for again.
// A read keeps the samples the previous overview knew (`carryOver`); only the newest of overlapping reads is applied.
// `session` frames update the VM link.
import type { TasksContext } from '../../tasks/pull';
import { sendErrorCode } from '../../tasks/send';
import { isRecord } from '../../registry';
import { HubError } from '../../net/signedFetch';
import { applySamples, carryOver, frameSamples, lists, type Overview, parseOverview } from '../../monitoring/overview';
import {
  type ChartData,
  chartData,
  type ChartSpec,
  parsePoints,
  type Point,
  type SeriesRequest,
  seriesKey,
  seriesTarget,
} from '../../monitoring/series';
import type { Selector, Slice, Store } from '../store';

export type MonitoringContext = Pick<TasksContext, 'store' | 'client' | 'now'>;

/** The overview is read again at most this often for entities it does not list yet. */
export const RELOAD_MS = 15_000;

export type LoadStatus = 'loading' | 'ready' | 'error';

export interface OrgMonitoring {
  readonly status: LoadStatus;
  readonly code: string | null;
  readonly overview: Overview | null;
  /** When the overview was last read (client clock), null before. */
  readonly loadedAt: number | null;
  /** A live sample named an entity the overview does not list (`pending` is not empty). */
  readonly unknown: boolean;
  /** Keys (`<kind> <id>`) of such entities since the last read. */
  readonly pending: readonly string[];
  /** Keys a read did not list although a sample had named them: not asked for again. */
  readonly skip: readonly string[];
}

export interface SeriesEntry {
  readonly status: LoadStatus;
  readonly code: string | null;
  readonly points: readonly Point[];
}

export interface MonitoringState {
  readonly orgs: Readonly<Record<string, OrgMonitoring>>;
  readonly series: Readonly<Record<string, SeriesEntry>>;
  /** Session → `vm_id` of its row (null: none or unreadable). */
  readonly links: Readonly<Record<string, string | null>>;
}

declare module '../store' {
  interface StoreState {
    monitoring: MonitoringState;
  }
}

export const slice: Slice<'monitoring'> = {
  name: 'monitoring',
  initial: () => ({ orgs: {}, series: {}, links: {} }),
};

const EMPTY: OrgMonitoring = {
  status: 'loading',
  code: null,
  overview: null,
  loadedAt: null,
  unknown: false,
  pending: [],
  skip: [],
};

/** The number of the newest overview read per store and org (only its answer is applied). */
const reads = new WeakMap<Store, Map<string, number>>();

export const orgMonitoring = (state: MonitoringState, org: string): OrgMonitoring => state.orgs[org] ?? EMPTY;

function setOrg(store: Store, org: string, change: (previous: OrgMonitoring) => OrgMonitoring): void {
  store.update('monitoring', (state) => {
    const previous = orgMonitoring(state, org);
    const next = change(previous);
    return next === previous ? state : { ...state, orgs: { ...state.orgs, [org]: next } };
  });
}

/** Reads the overview of `org` (`GET /v1/monitoring/overview`); the previous one stays visible while loading. */
export async function loadOverview(context: MonitoringContext, org: string): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const numbers = reads.get(store) ?? new Map<string, number>();
  reads.set(store, numbers);
  const number = (numbers.get(org) ?? 0) + 1;
  numbers.set(org, number);
  const asked = orgMonitoring(store.getState().monitoring, org).pending;
  const current = () => store.generation === generation && numbers.get(org) === number;
  setOrg(store, org, (previous) => ({ ...previous, status: previous.overview === null ? 'loading' : 'ready' }));
  try {
    const body = await context.client.request('GET', `/v1/monitoring/overview?org=${encodeURIComponent(org)}`);
    const read = parseOverview(body);
    if (!current()) return;
    setOrg(store, org, (previous) => {
      const overview = carryOver(previous.overview, read);
      const skip = [...new Set([...previous.skip, ...asked])].filter((key) => !lists(overview, key));
      const pending = previous.pending.filter((key) => !asked.includes(key) && !lists(overview, key));
      return {
        status: 'ready',
        code: null,
        overview,
        loadedAt: context.now(),
        unknown: pending.length > 0,
        pending,
        skip,
      };
    });
  } catch (error) {
    if (!current()) return;
    setOrg(store, org, (previous) => ({ ...previous, status: 'error', code: sendErrorCode(error) }));
  }
}

/** Reads one series (`GET /v1/monitoring/series`) into the slice under `seriesKey(request)`. */
export async function loadSeries(context: MonitoringContext, request: SeriesRequest): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const key = seriesKey(request);
  const put = (entry: SeriesEntry) =>
    store.update('monitoring', (state) => ({ ...state, series: { ...state.series, [key]: entry } }));
  const previous = store.getState().monitoring.series[key];
  if (previous?.status === 'ready' || previous?.status === 'loading') return;
  put({ status: 'loading', code: null, points: [] });
  // a chart that closed meanwhile dropped its entry: the answer is not stored again
  const wanted = () => store.generation === generation && Object.hasOwn(store.getState().monitoring.series, key);
  try {
    const points = parsePoints(await context.client.request('GET', seriesTarget(request)));
    if (wanted()) put({ status: 'ready', code: null, points });
  } catch (error) {
    if (wanted()) put({ status: 'error', code: sendErrorCode(error), points: [] });
  }
}

/** Drops the series under `keys` (a chart panel closed or moved to another window). */
export function dropSeries(store: Store, keys: Iterable<string>): void {
  const gone = new Set(keys);
  store.update('monitoring', (state) => {
    const kept = Object.entries(state.series).filter(([key]) => !gone.has(key));
    return kept.length === Object.keys(state.series).length ? state : { ...state, series: Object.fromEntries(kept) };
  });
}

export interface ChartView {
  readonly status: LoadStatus;
  /** The code of the first failed series. */
  readonly code: string | null;
  /** The chart data once every series is loaded. */
  readonly data: ChartData | null;
}

/**
 * The state of one chart (`spec`, one request per metric): loading until every series is loaded, the first failure,
 * or the aligned data. The selector returns the same object while the series entries stay the same.
 */
export function selectChart(spec: ChartSpec, requests: readonly SeriesRequest[]): Selector<ChartView> {
  let last: { readonly entries: readonly (SeriesEntry | undefined)[]; readonly view: ChartView } | null = null;
  return (state) => {
    const entries = requests.map((request) => state.monitoring.series[seriesKey(request)]);
    if (last !== null && entries.every((entry, index) => entry === last!.entries[index])) return last.view;
    const failed = entries.find((entry) => entry?.status === 'error');
    const ready = entries.every((entry) => entry?.status === 'ready');
    const points = Object.fromEntries(requests.map((request, index) => [request.metric, entries[index]?.points ?? []]));
    const view: ChartView = failed
      ? { status: 'error', code: failed.code, data: null }
      : ready
        ? { status: 'ready', code: null, data: chartData(spec, points) }
        : { status: 'loading', code: null, data: null };
    last = { entries, view };
    return view;
  };
}

function setLinks(store: Store, links: Readonly<Record<string, string | null>>): void {
  store.update('monitoring', (state) => ({ ...state, links: { ...state.links, ...links } }));
}

const vmOf = (row: unknown): string | null => (isRecord(row) && typeof row.vm_id === 'string' ? row.vm_id : null);

/** Sessions whose row is being read, per store (overlapping calls read each row once). */
const linking = new WeakMap<Store, Set<string>>();

/**
 * Reads the session rows of the sessions without a known VM (`GET /v1/sessions/{id}`), one after the other. A row the
 * hub refuses (`not_found`, `forbidden`) counts as "no VM"; after any other failure the session is read again next
 * time.
 */
export async function linkSessions(context: MonitoringContext, sessions: readonly string[]): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const busy = linking.get(store) ?? new Set<string>();
  linking.set(store, busy);
  for (const session of sessions) {
    if (Object.hasOwn(store.getState().monitoring.links, session) || busy.has(session)) continue;
    busy.add(session);
    let vm: string | null | undefined;
    try {
      const body = await context.client.request('GET', `/v1/sessions/${encodeURIComponent(session)}`);
      vm = isRecord(body) ? vmOf(body.session) : null;
    } catch (error) {
      vm = error instanceof HubError && (error.code === 'not_found' || error.code === 'forbidden') ? null : undefined;
    }
    busy.delete(session);
    if (store.generation !== generation) return;
    if (vm !== undefined) setLinks(store, { [session]: vm });
  }
}

export const frames = {
  /** `{samples:[{entity_kind, entity_id, sample}]}`: newer samples into the overview of the active org. */
  metrics: (store: Store, data: unknown) => {
    const org = store.getState().session.activeOrg;
    const samples = frameSamples(data);
    if (org === null || samples.length === 0) return;
    setOrg(store, org, (entry) => {
      if (entry.overview === null) return entry;
      const applied = applySamples(entry.overview, samples);
      const added = applied.unknown.filter((key) => !entry.skip.includes(key) && !entry.pending.includes(key));
      if (applied.overview === entry.overview && added.length === 0) return entry;
      const pending = [...entry.pending, ...new Set(added)];
      return { ...entry, overview: applied.overview, pending, unknown: pending.length > 0 };
    });
  },
  /** A session row after a change: its VM link. */
  session: (store: Store, data: unknown) => {
    if (!isRecord(data) || !isRecord(data.session) || typeof data.session.session_id !== 'string') return;
    const session = data.session.session_id;
    const vm = vmOf(data.session);
    if (store.getState().monitoring.links[session] === vm) return;
    setLinks(store, { [session]: vm });
  },
};
