// Every model subscription of the user at a glance (rule E-30, KOR-68, ADR-091; data of KOR-69, ADR-090; twin of
// `agent_platform/subscriptions.py` `subscriptions list`), measured centrally – nothing runs on the user's computer:
//
// - `GET /v1/me/subscriptions`: every `claude_subscription` record of the user with its newest reading (windows,
//   reading device, `unreadable`). Each record is fetched (`GET /v1/subscriptions/{id}`) and opened here like every own
//   record (owner, ID and kind of the header, author the user, the user's active agent or a device the verified logs
//   entitle to the user's `CK`, signature, `CK` of the record's epoch) for what only the owner may read: name, note,
//   address, whether it holds a token ("not logged in" without one) and its use. A record that does not open still
//   shows its reading (`unverifiable` with the code).
// - For the holder of the platform role `operator` only: the operator subscriptions that platform devices still read
//   from token files (KOR-50, ADR-081 section 6), from `GET /v1/orgs/{org}/quotas` of the organizations the verified
//   platform log grants them, once per label (the newest read wins); an organization whose answer fails is left out.
//   A token file named like an own record (ignoring case) is that subscription: only the record's row is listed, with
//   the file's reading when the record has none.
//   The hub gives only their binding window.
// - Whether a subscription is usable now, the window that binds it and the release of an exhausted one are never
//   computed here: they are the hub's, by the rule of ADR-081 section 6 (`agent_hub.models.quotas`) – `state` of each
//   record in `GET /v1/me/subscriptions`, the state and release (`until`) of a token file (`usage`, ADR-091 section 3).
// - `providerGroups` groups the rows by provider (`PROVIDERS`; an unknown provider ID shows itself, so a further
//   provider needs no change here) and shows the total of all its accounts, never the fullest account (`totalWindows`,
//   ADR-091 section 3): each window (5 hours, week, week per model, fullest) counts the accounts that measured it – the
//   share left is their mean, every account weighted equally (an account without that window does not count), the
//   reset the next one of them. The headline is the total of the week (else the fullest total window), with "usable
//   now: X of N" and the next release over the hub's states of its rows (counted here, as the summary of ADR-081
//   section 6 counts them: only the browser knows which rows are listed – names are sealed, a token file named like a
//   record is that record); the details are the total windows, then every account with all its windows.
//
// The readings carry the used share; the view shows what is left (`freePercent` of `credentials/quotas.ts`, shared
// with the settings sections: 100 % less the used share, at least 0 %, a window whose reset has passed free again)
// and colours it by that (30 % left or less amber, 10 % or less red, `freeLevel`). Nothing missing is shown as 0 %.
// The page is the owner's own: nobody else reads it.
import { parseJson } from '../encoding/json';
import { openCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import { meOrgs } from '../account/session';
import { parseOrgQuotas } from '../credentials/orgQuotas';
import { freePercent, orderedWindows, WINDOW_RE } from '../credentials/quotas';
import { listOwnKeys, type OwnKey } from '../credentials/records';
import { authorKey } from '../github/connection';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { type RequestsContext, RequestError, requestErrorCode } from '../requests/client';

export const KIND = 'claude_subscription';
export const OPERATOR_ROLE = 'operator';
/** The window id of an operator subscription whose hub reading names only its binding window (KOR-50). */
export const FULLEST = 'fullest';

/** A provider (driver) by ID: its display name. A further provider is one more entry; unknown IDs show themselves. */
export const PROVIDERS: Readonly<Record<string, { readonly name: string }>> = {
  claude: { name: 'Claude' },
  openai: { name: 'ChatGPT' },
};
export const providerName = (provider: string): string => PROVIDERS[provider]?.name ?? provider;

export interface QuotaWindow {
  /** `five_hour`, `seven_day`, `weekly:<model>` or `FULLEST`. */
  readonly window: string;
  readonly usedPercent: number;
  /** Unix ms, null when unknown. */
  readonly resetsAt: number | null;
}

export interface Reading {
  /** Unix ms of the read. */
  readonly fetchedAt: number;
  readonly deviceId: string | null;
  /** The usage endpoint refused the token (`fetchedAt` is the time of the refusal). */
  readonly unreadable: boolean;
  readonly windows: readonly QuotaWindow[];
}

export type Status = 'logged_in' | 'not_logged_in' | 'not_readable' | 'unverifiable' | 'measured';

export interface Use {
  readonly pool: boolean;
  readonly orgs: readonly string[];
}

/** The hub's state of a subscription (ADR-081 section 6; module comment). */
export interface Usage {
  /** `other`: an operator subscription that is neither (`blocked`, `deferred`, …): it counts as neither. */
  readonly state: 'usable' | 'exhausted' | 'other';
  /** The window that binds it now, null when none is named. */
  readonly binding: string | null;
  /** Unix ms of the release of an exhausted one, null when unknown. */
  readonly release: number | null;
}

export interface SubscriptionRow {
  /** The record ID, or `operator:<label>` for an operator subscription of a token file. */
  readonly key: string;
  readonly provider: string;
  readonly source: 'record' | 'operator';
  /** The record that serves the user's own sessions. */
  readonly primary: boolean;
  readonly name: string | null;
  readonly note: string | null;
  /** The address the subscription signed in with. */
  readonly email: string | null;
  readonly status: Status;
  /** The code of an `unverifiable` record. */
  readonly code: string | null;
  /** Which sessions besides the own ones may use it: `own` for the primary record, null when unknown. */
  readonly use: Use | 'own' | null;
  /** `usable`, `exhausted`, … of an operator subscription; null for a record. */
  readonly state: string | null;
  readonly reading: Reading | null;
  /** The hub's state, null without one (no reading, an older hub). */
  readonly usage: Usage | null;
  /** Unix ms of the newest change of the record, null for an operator subscription. */
  readonly changedAt: number | null;
}

const time = (value: unknown): number | null => {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
};

function quotaWindow(value: unknown): QuotaWindow | null {
  if (!isRecord(value) || typeof value.window !== 'string' || !WINDOW_RE.test(value.window)) return null;
  const used = value.used_percent;
  if (typeof used !== 'number' || !Number.isFinite(used) || used < 0) return null;
  const resetsAt = time(value.resets_at);
  if (value.resets_at !== null && resetsAt === null) return null;
  return { window: value.window, usedPercent: used, resetsAt };
}

/** The reading of a listed record, or null when there is none or it is malformed. */
export function reading(value: unknown): Reading | null {
  const fetchedAt = isRecord(value) ? time(value.fetched_at) : null;
  if (!isRecord(value) || fetchedAt === null || !Array.isArray(value.windows)) return null;
  return {
    fetchedAt,
    deviceId: typeof value.device_id === 'string' ? value.device_id : null,
    unreadable: value.unreadable === true,
    windows: orderedWindows(value.windows.map(quotaWindow).filter((window) => window !== null)),
  };
}

/** The hub's `state` of a listed record, or null when there is none or it is malformed. */
export function usage(value: unknown): Usage | null {
  if (!isRecord(value) || typeof value.usable !== 'boolean') return null;
  const binding = value.binding === 'five_hour' || value.binding === 'seven_day' ? value.binding : null;
  return { state: value.usable ? 'usable' : 'exhausted', binding, release: time(value.release) };
}

/** An item of `GET /v1/me/subscriptions`. */
export interface Listed {
  readonly id: string;
  readonly primary: boolean;
  readonly reading: Reading | null;
  readonly usage: Usage | null;
  readonly updatedAt: number | null;
}

/** The checked items of the list (a malformed answer is `unavailable`, a malformed item and a repeated ID are left
 *  out: nothing is listed twice). */
export function listedItems(body: unknown): Listed[] {
  if (!isRecord(body) || !Array.isArray(body.subscriptions)) throw new HubError('unavailable', 200);
  const seen = new Set<string>();
  return body.subscriptions.flatMap((item): Listed[] =>
    isRecord(item) &&
    typeof item.id === 'string' &&
    typeof item.primary === 'boolean' &&
    !seen.has(item.id) &&
    seen.add(item.id)
      ? [
          {
            id: item.id,
            primary: item.primary,
            reading: reading(item.reading),
            usage: usage(item.state),
            updatedAt: time(item.updated_at),
          },
        ]
      : [],
  );
}

const text = (value: unknown, most: number): string | null =>
  typeof value === 'string' && value.length > 0 && value.length <= most ? value : null;

function parseUse(value: unknown): Use | null {
  if (!isRecord(value) || typeof value.pool !== 'boolean' || !Array.isArray(value.orgs)) return null;
  return { pool: value.pool, orgs: value.orgs.filter((org): org is string => typeof org === 'string') };
}

/** What the opened body says: name, note, address, token or none, use (lenient: a field it cannot read is null). */
export interface Opened {
  readonly name: string | null;
  readonly note: string | null;
  readonly email: string | null;
  readonly token: boolean;
  readonly use: Use | null;
}

export function openedBody(body: unknown): Opened {
  if (!isRecord(body)) throw new RequestError('params_invalid');
  return {
    name: text(body.name, 64),
    note: text(body.note, 500),
    email: text(body.email, 254),
    token: typeof body.token === 'string' && body.token.length > 0,
    use: parseUse(body.use),
  };
}

/** Verifies and opens an own subscription record (module comment); `RequestError`/`CryptoError` with the code. */
export async function openSubscription(
  context: RequestsContext,
  id: string,
  answer: unknown,
  keys: readonly OwnKey[],
): Promise<Opened> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) throw new RequestError('unauthorized');
  const record = isRecord(answer) ? answer.record : null;
  const header = isRecord(record) && isRecord(record.header) ? (record.header as Fields) : {};
  if (header.id !== id || header.owner !== userId || header.kind !== KIND) throw new RequestError('header_invalid');
  const sigKey = authorKey(context.store, userId, identity, header.author);
  if (sigKey === null) throw new RequestError('author_not_writer');
  const checked = await verifyCredential(record, sigKey);
  const key = keys.find((found) => found.epoch === checked.epoch);
  if (key === undefined) throw new RequestError('epoch_stale');
  return openedBody(parseJson(await openCredential(key.key, record, sigKey)));
}

function recordRow(item: Listed, opened: Opened | null, code: string | null): SubscriptionRow {
  const status: Status =
    opened === null
      ? 'unverifiable'
      : !opened.token
        ? 'not_logged_in'
        : item.reading?.unreadable === true
          ? 'not_readable'
          : 'logged_in';
  return {
    key: item.id,
    provider: 'claude',
    source: 'record',
    primary: item.primary,
    name: opened?.name ?? null,
    note: opened?.note ?? null,
    email: opened?.email ?? null,
    status,
    code,
    use: item.primary ? 'own' : (opened?.use ?? (opened === null ? null : { pool: false, orgs: [] })),
    state: null,
    reading: item.reading,
    usage: item.usage,
    changedAt: item.updatedAt,
  };
}

/** The operator subscriptions of the token files, once per label (newest read), for the holder of the role. */
async function operatorRows(context: RequestsContext): Promise<SubscriptionRow[]> {
  const { userId, profile } = context.store.getState().me;
  const platform = context.store.getState().orgs.platform?.state ?? null;
  if (userId === null || platform === null || !platform.hasRole(userId, OPERATOR_ROLE)) return [];
  const granted = meOrgs(profile)
    .map((org) => org.org_id)
    .filter((org) => platform.operatorSubscribed(org));
  const newest = new Map<string, SubscriptionRow>();
  for (const org of granted) {
    // an organization whose quotas cannot be read (refused, an older hub, a malformed answer) is left out: it never
    // hides the user's own subscriptions or those of the other organizations
    const answer = await context.client
      .request('GET', `/v1/orgs/${encodeURIComponent(org)}/quotas`)
      .then(parseOrgQuotas)
      .catch(() => null);
    for (const sub of answer?.operator.subscriptions ?? []) {
      const known = newest.get(sub.label);
      if (known !== undefined && (known.reading?.fetchedAt ?? -1) >= (sub.fetchedAt ?? -1)) continue;
      newest.set(sub.label, {
        key: `operator:${sub.label}`,
        provider: 'claude',
        source: 'operator',
        primary: false,
        name: sub.label,
        note: null,
        email: null,
        status: sub.readable ? 'measured' : 'not_readable',
        code: null,
        use: { pool: true, orgs: [] },
        state: sub.state,
        reading:
          sub.fetchedAt === null
            ? null
            : {
                fetchedAt: sub.fetchedAt,
                deviceId: null,
                unreadable: !sub.readable,
                windows:
                  sub.usedPercent === null
                    ? []
                    : [{ window: FULLEST, usedPercent: sub.usedPercent, resetsAt: sub.resetsAt }],
              },
        // the hub's state of the file (ADR-081 section 6): `usable` and `exhausted` count, the others not
        usage: {
          state: sub.state === 'usable' || sub.state === 'exhausted' ? sub.state : 'other',
          binding: sub.usedPercent === null ? null : FULLEST,
          release: sub.state === 'exhausted' ? sub.until : null,
        },
        changedAt: null,
      });
    }
  }
  return [...newest.values()].sort((a, b) => Number(a.key > b.key) - Number(a.key < b.key));
}

export type LoadContext = RequestsContext & { readonly refreshLog: (org: string) => Promise<boolean> };

/** Every subscription of the user (module comment): the records first (primary, then by name), then the operator's. */
export async function loadSubscriptions(context: LoadContext): Promise<SubscriptionRow[]> {
  const listed = listedItems(await context.client.request('GET', '/v1/me/subscriptions'));
  const { userId, identity, profile } = context.store.getState().me;
  const rows: SubscriptionRow[] = [];
  if (listed.length > 0) {
    // the devices that log a subscription in write its record: their keys come from the verified logs
    for (const org of meOrgs(profile).map((item) => item.org_id))
      if (context.store.getState().orgs.byId[org]?.log == null) await context.refreshLog(org).catch(() => false);
    // without the own keys every record shows its reading as "cannot be opened" (epoch_stale), never nothing
    const keys =
      userId === null || identity === null
        ? []
        : await listOwnKeys(context, userId, identity).then(
            (found) => found.opening,
            () => [],
          );
    for (const item of listed) {
      try {
        const answer = await context.client.request('GET', `/v1/subscriptions/${encodeURIComponent(item.id)}`);
        rows.push(recordRow(item, await openSubscription(context, item.id, answer, keys), null));
      } catch (error) {
        if (error instanceof HubError && error.code === 'not_found') continue;
        rows.push(recordRow(item, null, requestErrorCode(error)));
      }
    }
  }
  rows.sort(
    (a, b) =>
      Number(b.primary) - Number(a.primary) ||
      (a.name ?? a.key).localeCompare(b.name ?? b.key) ||
      a.key.localeCompare(b.key),
  );
  // a token file whose name equals the name of an own record (ignoring case) is that subscription: the record's row
  // stays, with the file's reading when it has none of its own, and the file is not listed again (nothing twice)
  const byName = new Map(rows.flatMap((row) => (row.name === null ? [] : [[row.name.toLowerCase(), row] as const])));
  const files: SubscriptionRow[] = [];
  for (const file of await operatorRows(context)) {
    const record = byName.get(file.key.slice('operator:'.length).toLowerCase());
    if (record === undefined) files.push(file);
    else if (record.reading === null)
      rows[rows.indexOf(record)] = { ...record, reading: file.reading, usage: file.usage };
  }
  return [...rows, ...files];
}

// --- providers ---

/** The headline of a list of windows: the week, else the fullest window. */
function headlineIn<T extends QuotaWindow>(windows: readonly T[]): T | null {
  return (
    windows.find((window) => window.window === 'seven_day') ??
    [...windows].sort((a, b) => b.usedPercent - a.usedPercent)[0] ??
    null
  );
}

/** One window as the total of the accounts that measured it. */
export interface TotalWindow extends QuotaWindow {
  /** The mean share left of the accounts that measured it (each account's share left is at least 0 %). */
  readonly freePercent: number;
  /** How many accounts measured this window (at least 1). */
  readonly accounts: number;
  /** Unix ms of the oldest reading among them. */
  readonly measuredAt: number;
}

/** Each window as the total of the accounts that measured it (module comment), ordered: the used share and the share
 *  left (`freePercent` at `now`) are their means, every account weighted equally – an account without the window does
 *  not count –, the reset the next one of them (null when none of them knows it). Nothing measured gives no window. */
export function totalWindows(rows: readonly SubscriptionRow[], now: number): TotalWindow[] {
  const sums = new Map<
    string,
    { used: number; free: number; accounts: number; resetsAt: number | null; measuredAt: number }
  >();
  for (const row of rows) {
    if (row.reading === null) continue;
    const seen = new Set<string>();
    for (const window of row.reading.windows) {
      // one value per account and window: a repeated window of the same reading does not weigh twice
      if (seen.has(window.window)) continue;
      seen.add(window.window);
      const known = sums.get(window.window);
      const resets = [known?.resetsAt ?? null, window.resetsAt].filter((at) => at !== null);
      sums.set(window.window, {
        used: (known?.used ?? 0) + window.usedPercent,
        free: (known?.free ?? 0) + freePercent(window.usedPercent, window.resetsAt, now),
        accounts: (known?.accounts ?? 0) + 1,
        resetsAt: resets.length === 0 ? null : Math.min(...resets),
        measuredAt: Math.min(known?.measuredAt ?? Infinity, row.reading.fetchedAt),
      });
    }
  }
  return orderedWindows(
    [...sums.entries()].map(([window, sum]) => ({
      window,
      usedPercent: sum.used / sum.accounts,
      freePercent: sum.free / sum.accounts,
      resetsAt: sum.resetsAt,
      accounts: sum.accounts,
      measuredAt: sum.measuredAt,
    })),
  );
}

export interface ProviderGroup {
  readonly provider: string;
  readonly name: string;
  /** The total of the week of all accounts (else the fullest total window); null when nothing is measured. */
  readonly headline: TotalWindow | null;
  /** Unix ms of the oldest reading the headline counts, null without a headline. */
  readonly headlineAt: number | null;
  /** Each window as the total of the accounts that measured it, ordered. */
  readonly windows: readonly TotalWindow[];
  readonly rows: readonly SubscriptionRow[];
  /** How many of its accounts the hub counts as usable now (`Usage`, ADR-081 section 6). */
  readonly usable: number;
  /** Unix ms of the next release: the earliest future release among its exhausted accounts (as the summary of ADR-081
   *  section 6 names it); null when none waits for a known one. */
  readonly nextRelease: number | null;
  /** Unix ms of the newest read of any account, null when none was read. */
  readonly newest: number | null;
}

/** The rows grouped by provider with their totals; providers by ID, or with `sortByUsed` by the share left of the
 *  headline total (least left first, unknown last). */
export function providerGroups(rows: readonly SubscriptionRow[], now: number, sortByUsed = false): ProviderGroup[] {
  const groups = new Map<string, SubscriptionRow[]>();
  for (const row of rows) groups.set(row.provider, [...(groups.get(row.provider) ?? []), row]);
  const made = [...groups.entries()].map(([provider, members]): ProviderGroup => {
    const windows = totalWindows(members, now);
    const headline = headlineIn(windows);
    const reads = members.flatMap((row) => (row.reading === null ? [] : [row.reading.fetchedAt]));
    const releases = members.flatMap((row) =>
      row.usage?.state === 'exhausted' && row.usage.release !== null && row.usage.release > now
        ? [row.usage.release]
        : [],
    );
    return {
      provider,
      name: providerName(provider),
      headline,
      headlineAt: headline?.measuredAt ?? null,
      windows,
      rows: members,
      usable: members.filter((row) => row.usage?.state === 'usable').length,
      nextRelease: releases.length === 0 ? null : Math.min(...releases),
      newest: reads.length === 0 ? null : Math.max(...reads),
    };
  });
  const byId = (a: ProviderGroup, b: ProviderGroup) =>
    Number(a.provider > b.provider) - Number(a.provider < b.provider);
  const free = (group: ProviderGroup) => group.headline?.freePercent ?? Infinity;
  return made.sort(sortByUsed ? (a, b) => free(a) - free(b) || byId(a, b) : byId);
}

/** By what is left: `ok`, `warn` (30 % left or less, from 70 % used) or `low` (10 % left or less, from 90 % used). */
export const freeLevel = (freePercent: number): 'ok' | 'warn' | 'low' =>
  freePercent <= 10 ? 'low' : freePercent <= 30 ? 'warn' : 'ok';
/** The time until `at` as the local dashboard writes it: days and hours, hours and minutes, minutes, below a
 *  minute, or due. */
export function untilParts(
  at: number,
  now: number,
): { readonly kind: 'due' | 'soon' | 'min' | 'h' | 'd'; readonly a: number; readonly b: number } {
  const seconds = Math.floor((at - now) / 1000);
  if (seconds <= 0) return { kind: 'due', a: 0, b: 0 };
  if (seconds < 60) return { kind: 'soon', a: 0, b: 0 };
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return { kind: 'min', a: minutes, b: 0 };
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return { kind: 'h', a: hours, b: minutes % 60 };
  return { kind: 'd', a: Math.floor(hours / 24), b: hours % 24 };
}

/** The age of `at` in whole seconds, minutes, hours or days (a future time counts as 0 s). */
export function ageParts(at: number, now: number): { readonly unit: 's' | 'min' | 'h' | 'd'; readonly value: number } {
  const seconds = Math.max(0, Math.floor((now - at) / 1000));
  if (seconds < 60) return { unit: 's', value: seconds };
  if (seconds < 3600) return { unit: 'min', value: Math.floor(seconds / 60) };
  if (seconds < 86400) return { unit: 'h', value: Math.floor(seconds / 3600) };
  return { unit: 'd', value: Math.floor(seconds / 86400) };
}
