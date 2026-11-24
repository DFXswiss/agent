// The client's view of one board (06-web.md sections 1 and 6.1), built only from events the reader settled
// (04-crypto.md section 5.12) and the hub's entry rows:
//
// - verified `whiteboard.*` events of the board, ascending by `scope_seq`, applied with the hub's projection rules
//   (`agent_hub.whiteboards.projection`): an entry creates the entry `ref` (the first one wins, later ones with the same
//   ID are ignored); an edit replaces the text and a delete ends the entry, both only for an existing entry that is
//   not deleted. Every applied event is a version of the entry's history (author, time, text). An entry body whose
//   `level` disagrees with the header, or a body that is not valid, is shown as an unverifiable row (`header_invalid`);
// - unverifiable events whose unchecked header places them on the board: a neutral row with the code (e.g.
//   `author_not_writer` when the author lacked the right at `lh`), never content, never dropped;
// - locked events (the epoch key is not held, e.g. history before a grant without history): a neutral "cannot be
//   decrypted" row; pending events appear once the reader settled them;
// - an entry whose ID an earlier `whiteboard.entry` of the scope already claimed on another board, or one the hub does
//   not list although it existed when the rows were listed (an ID reused in another scope): the hub did not project it,
//   so it is a neutral `event_conflict` row, never an entry;
// - entries the hub lists without any event of them in the store (the rows are listed after the board's pull): a
//   "not available" row.
//
// Paged rows (ADR-086 section 2): the rows arrive page by page, newest first by the hub's order (`created_at DESC, id
// DESC`, where `created_at` is the receive time of the creating event). An entry missing from the rows read so far is
// only then "not listed" when its row would have been on one of those pages: when every page is read, or when the
// hub received its creating event (`received_at`, milliseconds) strictly after the `created_at` of the oldest row
// read – such a row sorts before the cursor in any case. Otherwise (no receive time, e.g. an own write read back, or
// an equal or older one) the entry is shown from its verified events until the page holding its row is read.
//
// Items are newest first by the position of the creating event (`scope_seq`; one board is one scope; a placeholder by
// the position of the newest event the hub names), ties by key. Readers verify the author's right through the reader
// (the kind matrix of `log/events.ts`).
import { isUuid7 } from '../encoding/ids';
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import type { EventEntry, ScopeEvents } from '../store/slices/events';
import type { WhiteboardRow } from '../store/slices/whiteboards';
import { type BoardLocation, onBoard } from './location';

export const ENTRY = 'whiteboard.entry';
export const EDIT = 'whiteboard.edit';
export const DELETE = 'whiteboard.delete';
export const WHITEBOARD_KINDS = [ENTRY, EDIT, DELETE] as const;
const KINDS: ReadonlySet<string> = new Set(WHITEBOARD_KINDS);

/** Markdown of an entry: 1–65 536 characters (`contracts/events/objects.json`, counted in code points). */
export const TEXT_MAX = 65536;

export function validText(value: unknown): value is string {
  if (typeof value !== 'string' || value === '') return false;
  let count = 0;
  for (const _ of value) {
    count += 1;
    if (count > TEXT_MAX) return false;
  }
  return true;
}

export interface Version {
  readonly eventId: string;
  readonly seq: number;
  readonly kind: string;
  readonly author: string;
  /** `header.ts` (Unix ms; 0 when not parseable). */
  readonly at: number;
  /** The text after this event; null for the delete. */
  readonly text: string | null;
}

export interface BoardEntry {
  readonly type: 'entry';
  readonly key: string;
  readonly id: string;
  readonly seq: number;
  readonly author: string;
  readonly createdAt: number;
  readonly updatedAt: number;
  /** The newest text (the last text before the delete for a deleted entry). */
  readonly text: string;
  readonly edited: boolean;
  readonly deleted: boolean;
  /** The newest applied event (an edit or delete names it as `base_event_id`). */
  readonly latestEventId: string;
  readonly versions: readonly Version[];
}

export interface BoardNeutral {
  readonly type: 'unverifiable' | 'locked';
  readonly key: string;
  readonly seq: number;
  readonly code: string | null;
  /** The entry the (unchecked) header names, if any. */
  readonly ref: string | null;
}

export interface BoardPlaceholder {
  readonly type: 'missing';
  readonly key: string;
  readonly seq: number;
  readonly ref: string;
}

export type BoardItem = BoardEntry | BoardNeutral | BoardPlaceholder;

export interface BoardView {
  /** Newest first. */
  readonly items: readonly BoardItem[];
  /** The entries by ID (also the deleted ones). */
  readonly entries: ReadonlyMap<string, BoardEntry>;
}

const rawHeader = (entry: EventEntry): Readonly<Record<string, unknown>> =>
  isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};

function timeOf(value: unknown): number {
  try {
    return parseTime(value);
  } catch {
    return 0;
  }
}

/** Unix ms of a hub time, null when there is none or it is not parseable. */
function hubTime(value: unknown): number | null {
  const at = timeOf(value);
  return at === 0 ? null : at;
}

const refOf = (header: Readonly<Record<string, unknown>>): string | null =>
  typeof header.ref === 'string' && header.ref !== '' ? header.ref : null;

interface Draft {
  id: string;
  seq: number;
  author: string;
  createdAt: number;
  updatedAt: number;
  text: string;
  edited: boolean;
  deleted: boolean;
  latestEventId: string;
  versions: Version[];
}

/** The exact keys of each body (`contracts/events/objects.json`, `additionalProperties: false`). */
const BODY_KEYS: Readonly<Record<string, string>> = {
  [ENTRY]: 'level,text',
  [EDIT]: 'base_event_id,text',
  [DELETE]: 'base_event_id',
};

/** Whether the body fits its kind's schema on this board (else the event is a neutral `header_invalid` row). */
function validBody(kind: string, body: unknown, location: BoardLocation): boolean {
  if (!isRecord(body) || Object.keys(body).sort().join(',') !== BODY_KEYS[kind]) return false;
  if (kind === ENTRY) return body.level === location.level && validText(body.text);
  return isUuid7(body.base_event_id) && (kind === DELETE || validText(body.text));
}

/** Applies one verified event to the drafts (hub rules, module comment). */
function apply(drafts: Map<string, Draft>, entry: EventEntry, header: Readonly<Record<string, unknown>>): void {
  const kind = header.kind as string;
  const ref = header.ref as string;
  const body = entry.body as Record<string, unknown>;
  const version: Version = {
    eventId: header.id as string,
    seq: entry.scopeSeq,
    kind,
    author: header.author as string,
    at: timeOf(header.ts),
    text: kind === DELETE ? null : (body.text as string),
  };
  const draft = drafts.get(ref);
  if (kind === ENTRY) {
    if (draft !== undefined) return;
    drafts.set(ref, {
      id: ref,
      seq: entry.scopeSeq,
      author: version.author,
      createdAt: version.at,
      updatedAt: version.at,
      text: version.text!,
      edited: false,
      deleted: false,
      latestEventId: version.eventId,
      versions: [version],
    });
    return;
  }
  if (draft === undefined || draft.deleted) return;
  draft.updatedAt = version.at;
  draft.latestEventId = version.eventId;
  draft.versions.push(version);
  if (kind === DELETE) draft.deleted = true;
  else {
    draft.text = version.text!;
    draft.edited = true;
  }
}

/**
 * The board at `location` from the events of its scope and the hub's rows. `checked` is null while the board loads;
 * once loaded it is the highest `scope_seq` the store held before the rows were listed: an entry created up to there
 * that the hub does not list was not projected by the hub (its ID was already used on another board or in another
 * scope) and is shown as a neutral `event_conflict` row. While `more` (older pages of rows are not read yet) that
 * holds only for an entry whose row would be on a read page (module comment). A listed entry without events is
 * missing.
 */
export function projectBoard(
  scope: ScopeEvents | undefined,
  location: BoardLocation,
  rows: readonly WhiteboardRow[],
  checked: number | null,
  more = false,
): BoardView {
  const drafts = new Map<string, Draft>();
  /** The hub's receive time of each entry's creating event (null when unknown). */
  const received = new Map<string, number | null>();
  const neutral: BoardNeutral[] = [];
  /** Entry IDs claimed by an earlier `whiteboard.entry` of the scope on any board (the first one wins, hub rule). */
  const claimed = new Set<string>();
  const events = scope === undefined ? [] : [...scope.entries.values()].sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const entry of events) {
    const header = entry.header ?? rawHeader(entry);
    if (!KINDS.has(header.kind as string) || entry.status === 'pending') continue;
    const item = { key: `n${entry.scopeSeq}`, seq: entry.scopeSeq, ref: refOf(header) };
    const here = onBoard(header, location);
    if (entry.status !== 'verified') {
      if (here)
        neutral.push({ ...item, type: entry.status === 'locked' ? 'locked' : 'unverifiable', code: entry.code });
      continue;
    }
    const kind = header.kind as string;
    const elsewhere = kind === ENTRY && claimed.has(item.ref!) && !drafts.has(item.ref!);
    if (kind === ENTRY) claimed.add(item.ref!);
    if (!here) continue;
    if (elsewhere) neutral.push({ ...item, type: 'unverifiable', code: 'event_conflict' });
    else if (!validBody(kind, entry.body, location))
      neutral.push({ ...item, type: 'unverifiable', code: 'header_invalid' });
    else {
      apply(drafts, entry, header);
      if (kind === ENTRY && !received.has(item.ref!)) received.set(item.ref!, hubTime(entry.receivedAt));
    }
  }
  const listed = new Set(rows.map((row) => row.entry_id));
  // the oldest row read: an entry the hub received strictly after it would be on a read page (module comment)
  const oldest = more ? hubTime(rows.at(-1)?.created_at) : null;
  const onReadPage = (id: string): boolean => {
    if (!more) return true;
    const at = received.get(id)!;
    return oldest !== null && at !== null && at > oldest;
  };
  const entries = new Map<string, BoardEntry>();
  for (const draft of drafts.values()) {
    if (checked !== null && draft.seq <= checked && !listed.has(draft.id) && onReadPage(draft.id)) {
      neutral.push({
        type: 'unverifiable',
        key: `n${draft.seq}`,
        seq: draft.seq,
        code: 'event_conflict',
        ref: draft.id,
      });
    } else {
      entries.set(draft.id, { ...draft, type: 'entry', key: `e${draft.id}` });
    }
  }
  const shown = new Set<string>([
    ...entries.keys(),
    ...neutral.flatMap((item) => (item.ref === null ? [] : [item.ref])),
  ]);
  const placeholders: BoardPlaceholder[] = rows
    .filter((row) => !shown.has(row.entry_id))
    .map((row) => ({
      type: 'missing',
      key: `p${row.entry_id}`,
      seq: row.latest_event.scope_seq,
      ref: row.entry_id,
    }));
  const items: BoardItem[] = [...entries.values(), ...neutral, ...placeholders].sort(
    (a, b) => b.seq - a.seq || Number(a.key > b.key) - Number(a.key < b.key),
  );
  return { items, entries };
}

/** What a neutral row says: the message key of the `whiteboard` catalog, its parameters and the code (if any). */
export interface NeutralLabel {
  readonly key: string;
  readonly params: Readonly<Record<string, string>>;
  readonly code: string | undefined;
}

/** The label of a neutral row; an unverifiable row always names a code (`unverifiable` if the reader gave none). */
export function neutralLabel(item: BoardNeutral | BoardPlaceholder): NeutralLabel {
  if (item.type !== 'unverifiable') return { key: `whiteboard.item.${item.type}`, params: {}, code: undefined };
  const code = item.code ?? 'unverifiable';
  return { key: 'whiteboard.item.unverifiable', params: { code }, code };
}
