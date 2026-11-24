// Held operations in the task's chat (KOR-25, ADR-066, `contracts/events/objects.json` `body_session_status`, third
// variant): the runner holds a `github.*` operation of a session instead of refusing it when the person who gave the
// instruction (the acting user, HOT-14, ADR-070) has no usable GitHub connection (`github_connection`) or when that
// connection does not reach the repository (`github_access`, KOR-65, rule E-29: with the one next step `next` –
// `{action, owner, url?}` of `contracts/events/objects.json` `github_next_step`) and writes one `session.status` event
// `{state:"waiting", waiting_for, operation_id, operation_type, repo?, for_user?, next?, until}`. `for_user` is that person: a personal connection is never used on behalf of another person, so
// only this person is asked to connect (a body without it, from an older runner, is addressed to the task owner). A repository outside a workgroup's
// optional repository list is refused as before (`repo_not_allowed`) – there is no approval card (no list means every
// repository of the connection, ADR-054). The chat shows it as a card with the platform's own text only:
//
// - `parseWaiting`: the body exactly as the contract allows it (no further key, a known reason, a UUIDv7, a dotted
//   type, `owner/name` of 03-api.md section 9, a next step of the closed list whose link leads to github.com (or the
//   loopback host of the doubles, which the URL pattern allows as the only http), a time). Anything else is no card – the session cannot slip text into it, and the step comes
//   from the device that runs the operation, never from the session; the chat shows a neutral row instead.
// - `foldWaiting`: waiting items of the same reason and addressee are one card at the place of the newest of them, with every
//   operation it stands for (an access card per owner of its step), the operation type of the newest item, the
//   repository of the newest item that named one
//   and the latest `until` (a session cannot flood the chat with cards); the step of the newest item that named one.
// - `waitStatus`: `done` once every operation of the card is registered at the hub or, for `github_connection`, once the
//   viewer is the addressee and has a GitHub connection made at or after the newest waiting item (an older one is the
//   connection the runner found unusable, expired or revoked, so the card offers to connect again; a new connection
//   does not settle `github_access` – the runner checks the repository again and registers the operation); `expired`
//   after `until` or when every registered operation was refused; else `open`.
import { UUID7_RE } from '../encoding/ids';
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import type { OperationRow } from '../store/slices/operations';
import { LOGIN_RE, URL_RE } from '../requests/bodies';
import { REPO_RE } from '../wg-settings/settings';
import type { TimelineItem } from '../chat/timeline';

export const WAIT_REASONS = ['github_connection', 'github_access'] as const;
export type WaitReason = (typeof WAIT_REASONS)[number];
export const NEXT_ACTIONS = [
  'connect',
  'install_app',
  'approve_app',
  'authorize_sso',
  'new_token',
  'check_access',
] as const;
export type NextAction = (typeof NEXT_ACTIONS)[number];

/** A link of a step leads to GitHub only (or the loopback host of the doubles): a device cannot put a look-alike
 *  login page on the card. */
export const onGithub = (url: string): boolean => url.startsWith('https://github.com/') || url.startsWith('http://');

/** The one step that gives the connection access to the repository (KOR-65). */
export interface NextStep {
  readonly action: NextAction;
  readonly owner: string;
  readonly url: string | null;
}

/** `next` exactly as the contract allows it, else null. */
export function parseNext(value: unknown): NextStep | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (
    !keys.includes('action') ||
    !keys.includes('owner') ||
    !keys.every((key) => ['action', 'owner', 'url'].includes(key))
  )
    return null;
  const { action, owner, url } = value;
  if (!(NEXT_ACTIONS as readonly unknown[]).includes(action)) return null;
  if (typeof owner !== 'string' || !LOGIN_RE.test(owner)) return null;
  if (url !== undefined && (typeof url !== 'string' || !URL_RE.test(url) || !onGithub(url))) return null;
  return { action: action as NextAction, owner, url: (url as string | undefined) ?? null };
}

/** `operation_type` of `contracts/common.json`. */
const TYPE_RE = /^[a-z][a-z_]*(\.[a-z][a-z_]*)+$/;
const REQUIRED = ['state', 'waiting_for', 'operation_id', 'operation_type', 'until'];
const OPTIONAL = ['repo', 'for_user', 'next'];

export interface Waiting {
  readonly reason: WaitReason;
  readonly operationId: string;
  readonly operationType: string;
  readonly repo: string | null;
  /** The person asked to connect (the acting user); null: the task owner (an older runner). */
  readonly forUser: string | null;
  /** The next step of `github_access`. */
  readonly next: NextStep | null;
  /** Unix ms. */
  readonly until: number;
}

/** A waiting body exactly as the contract allows it, else null (module comment). */
export function parseWaiting(body: unknown): Waiting | null {
  if (!isRecord(body) || body.state !== 'waiting') return null;
  const keys = Object.keys(body);
  if (
    !REQUIRED.every((key) => keys.includes(key)) ||
    !keys.every((key) => REQUIRED.includes(key) || OPTIONAL.includes(key))
  )
    return null;
  const { waiting_for: reason, operation_id: id, operation_type: type, repo, for_user: forUser } = body;
  if (!(WAIT_REASONS as readonly unknown[]).includes(reason)) return null;
  if (typeof id !== 'string' || !UUID7_RE.test(id)) return null;
  if (typeof type !== 'string' || type.length > 64 || !TYPE_RE.test(type)) return null;
  if (repo !== undefined && (typeof repo !== 'string' || !REPO_RE.test(repo))) return null;
  if (forUser !== undefined && (typeof forUser !== 'string' || !UUID7_RE.test(forUser))) return null;
  const next = body.next === undefined ? null : parseNext(body.next);
  if (body.next !== undefined && next === null) return null;
  let until: number;
  try {
    until = parseTime(body.until);
  } catch {
    return null;
  }
  return {
    reason: reason as WaitReason,
    operationId: id,
    operationType: type,
    repo: (repo as string | undefined) ?? null,
    forUser: (forUser as string | undefined) ?? null,
    next,
    until,
  };
}

/** One card: the waiting items of one kind (module comment). */
export interface WaitingCard {
  readonly reason: WaitReason;
  /** The repository of the newest item that named one. */
  readonly repo: string | null;
  /** The operation type of the newest item. */
  readonly operationType: string;
  /** The person asked to connect; null: the task owner. */
  readonly forUser: string | null;
  /** The next step of the newest item that named one (`github_access`). */
  readonly next: NextStep | null;
  /** Every operation the card stands for, oldest first. */
  readonly operations: readonly string[];
  /** The latest `until` (Unix ms). */
  readonly until: number;
  /** The time of the newest item (Unix ms). */
  readonly since: number;
}

/** Whether a timeline item is a `session.status` event of the `waiting` state. */
export const isWaitingItem = (item: TimelineItem): boolean =>
  item.kind === 'session.status' && item.source === 'event' && isRecord(item.body) && item.body.state === 'waiting';

/** The ordered timeline with the waiting items of one kind folded into one (module comment). */
export function foldWaiting(items: readonly TimelineItem[]): TimelineItem[] {
  const cards = new Map<string, { card: WaitingCard; last: number }>();
  const parsed = new Map<number, string>();
  items.forEach((item, index) => {
    const waiting = isWaitingItem(item) ? parseWaiting(item.body) : null;
    if (waiting === null) return;
    // a card per owner of an access step: steps for different owners (approve here, install there) both stay visible
    const key = `${waiting.reason}:${waiting.forUser ?? ''}:${waiting.next?.owner ?? ''}`;
    parsed.set(index, key);
    const known = cards.get(key)?.card;
    const operations = known?.operations ?? [];
    cards.set(key, {
      card: {
        reason: waiting.reason,
        repo: waiting.repo ?? known?.repo ?? null,
        operationType: waiting.operationType,
        forUser: waiting.forUser,
        next: waiting.next ?? known?.next ?? null,
        operations: operations.includes(waiting.operationId) ? operations : [...operations, waiting.operationId],
        until: Math.max(known?.until ?? 0, waiting.until),
        since: item.at,
      },
      last: index,
    });
  });
  const folded: TimelineItem[] = [];
  items.forEach((item, index) => {
    const key = parsed.get(index);
    if (key === undefined) folded.push(item);
    else if (cards.get(key)!.last === index) folded.push({ ...item, waiting: cards.get(key)!.card });
  });
  return folded;
}

export type WaitStatus = 'open' | 'done' | 'expired';

export interface WaitContext {
  readonly now: number;
  /** When the viewer's verified GitHub connection was made (Unix ms) if the viewer is the card's addressee, else null. */
  readonly connectedAt: number | null;
  readonly rows: Readonly<Record<string, OperationRow>>;
}

const REFUSED: ReadonlySet<string> = new Set(['rejected', 'denied', 'expired']);

/** The state of a card (module comment). */
export function waitStatus(card: WaitingCard, context: WaitContext): WaitStatus {
  if (card.reason === 'github_connection' && context.connectedAt !== null && context.connectedAt >= card.since)
    return 'done';
  const rows = card.operations.map((id) => context.rows[id]).filter((row) => row !== undefined);
  if (rows.length === card.operations.length) return rows.every((row) => REFUSED.has(row.state)) ? 'expired' : 'done';
  return context.now >= card.until ? 'expired' : 'open';
}
