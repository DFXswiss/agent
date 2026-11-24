// Credit and usage charges (12-billing.md sections 6 and 9, ADR-062): the organization's balance and history
// (`GET /v1/orgs/{org}/billing`) and the charges of a task (`GET /v1/tasks/{task}/charges`). Amounts arrive in
// micro-USD. There is no payment method and no top-up: the operator adds credit, the web only shows it. A malformed
// answer is `unavailable` (the hub of an older release has no billing routes and answers `not_found`).
//
// Both answers may carry `people` (ADR-083 section 2): the consumption per person who triggered it, `user_id` null for
// unattributed consumption. The charges stay booked to the organization; an older hub sends no `people` (null here,
// nothing shown). The people of a task come with its newest page.
//
// The charges of a task are read page by page (ADR-086 section 2, `core/org/pages.ts`): newest session first, the
// next page on "Load more"; a hub without paging answers the whole list (cut at its cap with `truncated`).
import { HubError } from '../net/signedFetch';
import { LIMIT, type ListPage, readHubPage } from '../org/pages';
import { isRecord } from '../registry';
import type { RequestsContext } from '../requests/client';

export interface Consumption {
  readonly tokensMicroUsd: number;
  readonly runtimeMicroUsd: number;
  readonly tokens: number;
  readonly runtimeMs: number;
}

export interface CreditEntry {
  readonly at: string;
  readonly kind: string;
  readonly amountMicroUsd: number;
  readonly balanceMicroUsd: number;
  readonly reason: string | null;
}

export interface PriceRow {
  readonly label: string;
  readonly input: number;
  readonly output: number;
}

/** The consumption of one person (`user_id` null: not attributed). */
export interface PersonConsumption extends Consumption {
  readonly userId: string | null;
}

export interface OrgBilling {
  readonly balanceMicroUsd: number;
  readonly exhausted: boolean;
  readonly active: boolean;
  readonly version: number | null;
  readonly tokenPrices: readonly PriceRow[];
  readonly hourly: readonly { readonly size: string; readonly usd: number }[];
  readonly credits: readonly CreditEntry[];
  readonly days: readonly (Consumption & { readonly day: string })[];
  readonly workgroups: readonly (Consumption & { readonly wgId: string })[];
  /** Absent from an older hub. */
  readonly people: readonly PersonConsumption[] | null;
  /** Older credit entries exist than the newest ones in `credits`. */
  readonly creditsTruncated: boolean;
}

export interface SessionCharges extends Consumption {
  readonly sessionId: string;
}

/** One page of the charges of a task; the newest page carries the total of the whole task. */
export interface TaskChargesPage extends ListPage<SessionCharges> {
  /** The total of the whole task (null on an older page that does not say it). */
  readonly totalMicroUsd: number | null;
  /** The consumption per person of the whole task (null from an older hub and on an older page). */
  readonly people: readonly PersonConsumption[] | null;
}

const num = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : NaN);

function consumption(value: Record<string, unknown>): Consumption | null {
  const found = {
    tokensMicroUsd: num(value.tokens_micro_usd),
    runtimeMicroUsd: num(value.runtime_micro_usd),
    tokens: num(value.tokens),
    runtimeMs: num(value.runtime_ms),
  };
  return Object.values(found).every(Number.isFinite) ? found : null;
}

function rows<T>(value: unknown, make: (row: Record<string, unknown>) => T | null): T[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((row) => {
    const made = isRecord(row) ? make(row) : null;
    return made === null ? [] : [made];
  });
}

/** The `people` list of an answer (rows with a malformed person or amount are left out), null when absent. */
export function people(value: unknown): PersonConsumption[] | null {
  if (!Array.isArray(value)) return null;
  return rows(value, (row) => {
    const found = consumption(row);
    const user = row.user_id;
    return found !== null && (user === null || typeof user === 'string') ? { ...found, userId: user } : null;
  });
}

/** The customer prices of the price list in force: per million tokens after the factor, per VM hour. */
export function priceRows(document: unknown): Pick<OrgBilling, 'tokenPrices' | 'hourly'> {
  if (!isRecord(document) || !isRecord(document.tokens) || !isRecord(document.runtime)) {
    return { tokenPrices: [], hourly: [] };
  }
  const { tokens, runtime } = document;
  const factors = isRecord(tokens.factors) ? tokens.factors : {};
  const factor = num(factors.platform);
  const models = isRecord(tokens.models) ? tokens.models : {};
  const tokenPrices = Object.entries(models).flatMap(([label, price]) => {
    if (!isRecord(price)) return [];
    const input = num(price.input) * factor;
    const output = num(price.output) * factor;
    return Number.isFinite(input) && Number.isFinite(output) ? [{ label, input, output }] : [];
  });
  const hours = isRecord(runtime.per_hour) ? runtime.per_hour : {};
  const hourly = Object.entries(hours).flatMap(([size, usd]) =>
    size !== 'default' && Number.isFinite(num(usd)) ? [{ size, usd: num(usd) }] : [],
  );
  return { tokenPrices, hourly };
}

/** The answer of `GET /v1/orgs/{org}/billing`, or null when it is malformed. */
export function orgBilling(body: unknown): OrgBilling | null {
  if (!isRecord(body) || !Number.isFinite(num(body.balance_micro_usd))) return null;
  if (typeof body.exhausted !== 'boolean' || typeof body.billing_active !== 'boolean') return null;
  const list = isRecord(body.price_list) ? body.price_list : null;
  return {
    balanceMicroUsd: num(body.balance_micro_usd),
    exhausted: body.exhausted,
    active: body.billing_active,
    version: list === null ? null : num(list.version),
    ...priceRows(list === null ? null : list.body),
    credits: rows(body.credits, (row) =>
      typeof row.at === 'string' && typeof row.kind === 'string'
        ? {
            at: row.at,
            kind: row.kind,
            amountMicroUsd: num(row.amount_micro_usd),
            balanceMicroUsd: num(row.balance_micro_usd),
            reason: typeof row.reason === 'string' ? row.reason : null,
          }
        : null,
    ),
    days: rows(body.days, (row) => {
      const found = consumption(row);
      return found !== null && typeof row.day === 'string' ? { ...found, day: row.day } : null;
    }),
    workgroups: rows(body.workgroups, (row) => {
      const found = consumption(row);
      return found !== null && typeof row.wg_id === 'string' ? { ...found, wgId: row.wg_id } : null;
    }),
    people: people(body.people),
    creditsTruncated: body.credits_truncated === true,
  };
}

/** A session row of `GET /v1/tasks/{task}/charges`, or null when it is malformed. */
export function sessionCharges(row: Record<string, unknown>): SessionCharges | null {
  const found = consumption(row);
  return found !== null && typeof row.session_id === 'string' ? { ...found, sessionId: row.session_id } : null;
}

export async function loadOrgBilling(context: RequestsContext, org: string): Promise<OrgBilling> {
  const found = orgBilling(await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/billing`));
  if (found === null) throw new HubError('unavailable', 200);
  return found;
}

/** The page of the task's charges below `before` (null: the newest), or the whole list on a hub without paging. */
export async function loadTaskChargesPage(
  context: RequestsContext,
  task: string,
  before: string | null,
): Promise<TaskChargesPage> {
  const page = await readHubPage(
    context.client,
    `/v1/tasks/${encodeURIComponent(task)}/charges`,
    'sessions',
    before,
    LIMIT,
  );
  const total = num(page.body.total_micro_usd);
  // the newest page must say the total (the older page of a hub without paging is empty and says nothing)
  if (before === null && !Number.isFinite(total)) throw new HubError('unavailable', 200);
  return {
    rows: rows(page.items, sessionCharges),
    nextBefore: page.nextBefore,
    truncated: page.truncated,
    totalMicroUsd: Number.isFinite(total) ? total : null,
    people: people(page.body.people),
  };
}

/** micro-USD as dollars: two decimals, four below one cent so small charges are not shown as $0.00. */
export function formatUsd(micro: number, locale: string): string {
  const usd = micro / 1_000_000;
  const small = usd !== 0 && Math.abs(usd) < 0.01;
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: small ? 4 : 2,
    maximumFractionDigits: small ? 4 : 2,
  }).format(usd);
}

/** A runtime in milliseconds as `1 h 2 min 3 s` (seconds below a minute). */
export function formatRuntime(ms: number): string {
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const parts = [hours > 0 ? `${hours} h` : '', minutes > 0 ? `${minutes} min` : '', seconds > 0 ? `${seconds} s` : ''];
  const text = parts.filter((part) => part !== '').join(' ');
  return text === '' ? '0 s' : text;
}
