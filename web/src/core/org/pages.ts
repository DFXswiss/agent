// Pages of the hub's bounded organization lists (ADR-086 section 2; 03-api.md sections 4.2 and 4.5):
// `GET /v1/orgs/{org}/invites`, `GET /v1/platform/invites`, `GET /v1/orgs/{org}/devices` and
// `GET /v1/workgroups/{wg}/device-candidates` (and the charges of a task, `core/billing`) take optional `before` (the opaque `next_before` of the previous page)
// and `limit` (at most `LIMIT_MAX`) and answer one page newest first with `next_before` (null at the end). Without the
// parameters a hub answers the whole list up to its cap, with `truncated: true` when the cap cut it.
//
// The views read the first page and the next one on "Load more" – never every page in a loop. A view that reads its
// list again (a change, a revocation, the presence poll of the device list) reads the range it has loaded so far: the
// same number of pages from the newest one (`readPages`), so rows loaded by "Load more" stay current and a removed
// row disappears.
//
// An old hub does not know the parameters and answers them with `400 request_invalid`. The client then repeats the
// request once without them – the old whole answer (`next_before` absent: the end) – and remembers for this hub (per
// signed client, i.e. per loaded configuration) that the lists are not paged.
import { HubError, type SignedClient } from '../net/signedFetch';
import { isRecord } from '../registry';

/** The page size of the invitation lists (the window shows two pages of 50 at first). */
export const LIMIT = 100;
/** The largest page the hub serves (the device lists use it: a presence poll re-reads the loaded range). */
export const LIMIT_MAX = 200;

/** One page of a list. */
export interface ListPage<T> {
  readonly rows: T[];
  /** `before` of the next older page; null when the list ends here (or the hub does not page). */
  readonly nextBefore: string | null;
  /** The hub cut its unpaged answer at its cap (only an unpaged answer says so). */
  readonly truncated: boolean;
}

/** A page as the hub sent it: the named array's items (for the caller's own check) and the paging fields. */
export interface HubPage {
  readonly items: readonly unknown[];
  readonly nextBefore: string | null;
  readonly truncated: boolean;
  /** The whole answer (its other fields, such as a total; empty for the older page of an old hub). */
  readonly body: Readonly<Record<string, unknown>>;
}

type Client = Pick<SignedClient, 'request'>;

/** Hubs (signed clients) that refused `before`/`limit` on a list. */
const unpaged = new WeakSet<object>();

/** Whether the hub behind `client` is known not to page the organization lists (old hub). */
export const lacksListPaging = (client: Client): boolean => unpaged.has(client);

function hubPage(body: unknown, key: string): HubPage {
  if (!isRecord(body) || !Array.isArray(body[key])) throw new HubError('unavailable', 200);
  const next = body.next_before;
  // absent: an unpaged answer (the whole list)
  if (!(next === undefined || next === null || typeof next === 'string')) throw new HubError('unavailable', 200);
  return { items: body[key] as unknown[], nextBefore: next ?? null, truncated: body.truncated === true, body };
}

/**
 * The page of `path` below `before` (null: the newest), at most `limit` items of the array `key`; on an old hub the
 * whole list (module comment). The caller checks the items (`unavailable` for a body without the array).
 */
export async function readHubPage(
  client: Client,
  path: string,
  key: string,
  before: string | null,
  limit: number,
): Promise<HubPage> {
  if (!unpaged.has(client)) {
    const params = new URLSearchParams();
    if (before !== null) params.set('before', before);
    params.set('limit', String(limit));
    try {
      return hubPage(await client.request('GET', `${path}?${params.toString()}`), key);
    } catch (error) {
      if (!(error instanceof HubError && error.code === 'request_invalid')) throw error;
      unpaged.add(client);
    }
  }
  // an old hub has only the whole list: an older page of it is empty
  if (before !== null) return { items: [], nextBefore: null, truncated: false, body: {} };
  return hubPage(await client.request('GET', path), key);
}

/**
 * The first `count` pages of a list (at least one), following `nextBefore`, each row once; stops at the end. `pages`
 * is how many were read – the range a later re-read covers.
 */
export async function readPages<T>(
  read: (before: string | null) => Promise<ListPage<T>>,
  count: number,
  id: (row: T) => string,
): Promise<ListPage<T> & { readonly pages: number }> {
  let page = await read(null);
  let rows = page.rows;
  let truncated = page.truncated;
  let pages = 1;
  while (pages < count && page.nextBefore !== null) {
    page = await read(page.nextBefore);
    rows = appendRows(rows, page.rows, id);
    truncated ||= page.truncated;
    pages += 1;
  }
  return { rows, nextBefore: page.nextBefore, truncated, pages };
}

/** `older` appended to `loaded`, without rows `loaded` already holds (a row moved between two pages). */
export function appendRows<T>(loaded: readonly T[], older: readonly T[], id: (row: T) => string): T[] {
  const known = new Set(loaded.map(id));
  return [...loaded, ...older.filter((row) => !known.has(id(row)))];
}
