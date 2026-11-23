// Names and titles for the task screens, read from verified events only (06-web.md section 1: unverifiable items are
// shown as such, never as content):
// - the org scope: `org.profile` (org name), `wg.profile` (workgroup names, `header.wg`), `member.profile` (display
//   names, `ref` = author); the newest verified event of each wins;
// - a workgroup scope: the task titles, `task.meta` of a task (the row's `title_event_id` when present, else the
//   newest verified one of the task, never another task's event; a session's `task.set_title` writes a newer one);
// - the owner of a task: the author of its first verified user-authored `task.meta` (the one `POST /v1/tasks` sends
//   with the task), in `scope_seq` order, never the hub's `owner_id` metadata – a hub could name anyone (ADR-003;
//   KOR-04, AUD-01 A-16). Not the signed `ts`: its writer chooses it, so a writer of the workgroup could backdate a
//   `task.meta` of someone else's task. Device-authored ones (`task.set_title`) are skipped; an author the verified org
//   state does not know ends the search (no owner rather than a later one).
import { DEVICE, USER } from '../log/state';
import { orgEntry } from '../store/slices/orgs';
import { memoize } from '../store/selectors';
import type { EventEntry, ScopeEvents } from '../store/slices/events';
import { isRecord } from '../registry';
import type { StoreState } from '../store/store';

export const PROFILE_KINDS = ['org.profile', 'wg.profile', 'member.profile'] as const;
export const TITLE_KIND = 'task.meta';

export interface Directory {
  readonly orgName: string | null;
  /** Workgroup ID → name. */
  readonly workgroups: ReadonlyMap<string, string>;
  /** Principal ID → display name. */
  readonly members: ReadonlyMap<string, string>;
}

const EMPTY: Directory = { orgName: null, workgroups: new Map(), members: new Map() };

const text = (body: unknown, field: string): string | null =>
  isRecord(body) && typeof body[field] === 'string' ? body[field] : null;

/** Verified entries ascending, so that later ones overwrite earlier ones. */
const verified = (scope: ScopeEvents): EventEntry[] =>
  [...scope.entries.values()].filter((entry) => entry.status === 'verified').sort((a, b) => a.scopeSeq - b.scopeSeq);

const directoryOf = memoize((scope: ScopeEvents): Directory => {
  let orgName: string | null = null;
  const workgroups = new Map<string, string>();
  const members = new Map<string, string>();
  for (const entry of verified(scope)) {
    const header = entry.header!;
    if (header.kind === 'org.profile') orgName = text(entry.body, 'name') ?? orgName;
    if (header.kind === 'wg.profile') {
      const name = text(entry.body, 'name');
      if (name !== null) workgroups.set(header.wg as string, name);
    }
    if (header.kind === 'member.profile' && header.ref === header.author) {
      const name = text(entry.body, 'display_name');
      if (name !== null) members.set(header.ref as string, name);
    }
  }
  return { orgName, workgroups, members };
});

/** The names of an org from its org scope (empty until the profiles were read). */
export const selectDirectory =
  (org: string) =>
  (state: StoreState): Directory => {
    const scope = state.events[org];
    return scope === undefined ? EMPTY : directoryOf(scope);
  };

/** The title of a task: `verified` with the text, `unverifiable` with the code, or `missing` (not read yet). */
export type Title =
  | { readonly status: 'verified'; readonly text: string }
  | { readonly status: 'unverifiable'; readonly code: string }
  | { readonly status: 'missing' };

const MISSING: Title = { status: 'missing' };

interface TitleIndex {
  /** Event ID → the task the event belongs to (verified events) and its title. */
  readonly byEvent: ReadonlyMap<string, { readonly task: string | null; readonly title: Title }>;
  /** Task ID → newest verified title. */
  readonly byTask: ReadonlyMap<string, Title>;
  /** Task ID → the authors of its verified `task.meta` events in `scope_seq` order. */
  readonly authors: ReadonlyMap<string, readonly string[]>;
}

const titlesOf = memoize((scope: ScopeEvents): TitleIndex => {
  const byEvent = new Map<string, { task: string | null; title: Title }>();
  const byTask = new Map<string, Title>();
  const authors = new Map<string, string[]>();
  const entries = [...scope.entries.values()].sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const entry of entries) {
    if (entry.status === 'unverifiable') {
      const id = isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header.id : null;
      if (typeof id === 'string') byEvent.set(id, { task: null, title: { status: 'unverifiable', code: entry.code! } });
      continue;
    }
    if (entry.status !== 'verified' || entry.header!.kind !== TITLE_KIND) continue;
    const title: Title = { status: 'verified', text: text(entry.body, 'title') ?? '' };
    byEvent.set(entry.header!.id as string, { task: entry.header!.task as string, title });
    byTask.set(entry.header!.task as string, title);
    const task = entry.header!.task as string;
    const known = authors.get(task);
    if (known === undefined) authors.set(task, [entry.header!.author as string]);
    else known.push(entry.header!.author as string);
  }
  return { byEvent, byTask, authors };
});

/** The title of `task` in workgroup `wg` (`titleEventId` from the task row). */
export const selectTitle =
  (wg: string, task: string, titleEventId: string | null) =>
  (state: StoreState): Title => {
    const scope = state.events[wg];
    if (scope === undefined) return MISSING;
    const index = titlesOf(scope);
    // the hub names the title event: a verified one counts only if it belongs to this task (a hub could point at
    // another task's genuine title); an unverifiable one is shown as such
    const named = titleEventId === null ? undefined : index.byEvent.get(titleEventId);
    const own = named !== undefined && (named.task === null || named.task === task) ? named.title : undefined;
    return own ?? index.byTask.get(task) ?? MISSING;
  };

/**
 * The owner of `task` in workgroup `wg` of `org` (module comment): the author of the task's first verified `task.meta`
 * that a user of the verified org state wrote; null while none is known.
 */
export const selectOwner =
  (org: string, wg: string, task: string) =>
  (state: StoreState): string | null => {
    const scope = state.events[wg];
    const principals = orgEntry(state.orgs, org).log?.state.principals;
    if (scope === undefined || principals === undefined) return null;
    for (const author of titlesOf(scope).authors.get(task) ?? []) {
      const kind = principals.get(author)?.kind;
      if (kind !== DEVICE) return kind === USER ? author : null;
    }
    return null;
  };
