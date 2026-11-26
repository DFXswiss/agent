// Names and titles for the task screens, read from verified events only (06-web.md section 1: unverifiable items are
// shown as such, never as content):
// - the org scope: `org.profile` (org name), `wg.profile` (workgroup names, `header.wg`), `member.profile` (display
//   names, `ref` = author), `member.name` (a name an admin gave, `ref` = a member or an invitation, ADR-073); the
//   verified event with the highest `scope_seq` of each wins. A member's name is its own `member.profile` first, else
//   the admin-given name of the member, else the admin-given name of the invitation its current membership started
//   with (the log's `accept`); an agent of the verified log is named through its user (`agents`, "Agent of <name>",
//   ADR-082 section 1); an admin-given name counts only while its author is an admin in the current verified
//   log and – for a member – only from the member's current membership on (`lh` ≥ its `joinedSeq`). Only the own name
//   is the member's own (`ownNames`: proposed when joining another org, written into an org opened without one);
// - a workgroup scope: the task titles, `task.meta` of a task (the row's `title_event_id` when present, else the
//   newest verified one of the task, never another task's event; a session's `task.set_title` writes a newer one);
// - the owner of a task: the author of its first verified `task.meta` written by a user or an agent (the one
//   `POST /v1/tasks` sends with the task; an agent is shown as the agent of its user), in `scope_seq` order, never the hub's `owner_id` metadata – a hub could name anyone (ADR-003;
//   KOR-04, AUD-01 A-16). Not the signed `ts`: its writer chooses it, so a writer of the workgroup could backdate a
//   `task.meta` of someone else's task. Device-authored ones (`task.set_title`) are skipped; an author the verified org
//   state does not know ends the search (no owner rather than a later one).
import { AGENT, DEVICE, type OrgState, USER } from '../log/state';
import { orgEntry } from '../store/slices/orgs';
import { memoize, sameAs, scopeIdOf } from '../store/selectors';
import type { EventEntry, ScopeEvents } from '../store/slices/events';
import { isRecord } from '../registry';
import type { StoreState } from '../store/store';

export const MEMBER_NAME = 'member.name';
export const PROFILE_KINDS = ['org.profile', 'wg.profile', 'member.profile', MEMBER_NAME] as const;
export const TITLE_KIND = 'task.meta';

/** Where a member's shown name comes from: its own profile, an admin (`by`), or its invitation's name (`by`). */
export interface NameSource {
  readonly source: 'own' | 'admin' | 'invitation';
  /** The admin who gave the name (null for the own name). */
  readonly by: string | null;
}

/** A name an admin gave (`member.name`). */
export interface GivenName {
  readonly name: string;
  readonly by: string;
}

interface GivenEvent extends GivenName {
  /** The log head the event was written at. */
  readonly lh: number;
}

export interface Directory {
  readonly orgName: string | null;
  /** Workgroup ID → name. */
  readonly workgroups: ReadonlyMap<string, string>;
  /** Principal ID → the name to show (module comment). */
  readonly members: ReadonlyMap<string, string>;
  /** Principal ID → the member's own display name (`member.profile`). */
  readonly ownNames: ReadonlyMap<string, string>;
  /** Principal ID → where the name in `members` comes from. */
  readonly sources: ReadonlyMap<string, NameSource>;
  /** Invitation ID → the name an admin gave the invited person. */
  readonly invitations: ReadonlyMap<string, GivenName>;
  /** Agent ID → its user, for every agent of the verified log (shown as "Agent of <user>", ADR-082 section 1). */
  readonly agents: ReadonlyMap<string, string>;
}

/** A directory without any name (also the start of test fixtures). */
export const EMPTY_DIRECTORY: Directory = {
  orgName: null,
  workgroups: new Map(),
  members: new Map(),
  ownNames: new Map(),
  sources: new Map(),
  invitations: new Map(),
  agents: new Map(),
};

const text = (body: unknown, field: string): string | null =>
  isRecord(body) && typeof body[field] === 'string' ? body[field] : null;

/** Verified entries ascending, so that later ones overwrite earlier ones. */
const verified = (scope: ScopeEvents): EventEntry[] =>
  [...scope.entries.values()].filter((entry) => entry.status === 'verified').sort((a, b) => a.scopeSeq - b.scopeSeq);

interface Profiles {
  readonly orgName: string | null;
  readonly workgroups: ReadonlyMap<string, string>;
  readonly own: ReadonlyMap<string, string>;
  /** `ref` → the admin-given names in `scope_seq` order (the verifier checked E5: an admin at `lh`). */
  readonly given: ReadonlyMap<string, readonly GivenEvent[]>;
}

const profilesOf = memoize((scope: ScopeEvents): Profiles => {
  let orgName: string | null = null;
  const workgroups = new Map<string, string>();
  const own = new Map<string, string>();
  const given = new Map<string, GivenEvent[]>();
  for (const entry of verified(scope)) {
    const header = entry.header!;
    if (header.kind === 'org.profile') orgName = text(entry.body, 'name') ?? orgName;
    if (header.kind === 'wg.profile') {
      const name = text(entry.body, 'name');
      if (name !== null) workgroups.set(header.wg as string, name);
    }
    const name = text(entry.body, 'display_name');
    if (name === null) continue;
    if (header.kind === 'member.profile' && header.ref === header.author) own.set(header.ref as string, name);
    if (header.kind === MEMBER_NAME) {
      const named = { name, by: header.author as string, lh: header.lh as number };
      given.set(header.ref as string, [...(given.get(header.ref as string) ?? []), named]);
    }
  }
  return { orgName, workgroups, own, given };
});

/** The newest of `events` by an admin of `state` written at `lh` ≥ `from` (any author without a verified log). */
function newest(
  events: readonly GivenEvent[] | undefined,
  state: OrgState | null,
  from: number,
): GivenName | undefined {
  const valid = (events ?? []).filter((event) => state === null || (state.isAdmin(event.by) && event.lh >= from));
  const found = valid.at(-1);
  return found === undefined ? undefined : { name: found.name, by: found.by };
}

/** The names of the org's users (module comment); without a verified log only the own and directly given names. */
function directoryOf(profiles: Profiles, state: OrgState | null): Directory {
  const members = new Map(profiles.own);
  const sources = new Map<string, NameSource>([...profiles.own.keys()].map((id) => [id, { source: 'own', by: null }]));
  const invitations = new Map<string, GivenName>();
  const give = (id: string, name: GivenName | undefined, source: NameSource['source']) => {
    if (name === undefined || members.has(id)) return;
    members.set(id, name.name);
    sources.set(id, { source, by: name.by });
  };
  const { orgName, workgroups, own: ownNames } = profiles;
  if (state === null) {
    for (const [ref, events] of profiles.given) give(ref, newest(events, null, 0), 'admin');
    return { orgName, workgroups, members, ownNames, sources, invitations, agents: new Map() };
  }
  for (const invite of state.invites.values()) {
    const name = newest(profiles.given.get(invite.inviteId), state, 0);
    if (invite.kind === USER && name !== undefined) invitations.set(invite.inviteId, name);
  }
  const users = [...state.principals.values()].filter((p) => p.kind === USER);
  for (const user of users) give(user.id, newest(profiles.given.get(user.id), state, user.joinedSeq), 'admin');
  const joinedBy = new Map(users.map((user) => [user.joinedSeq, user.id]));
  for (const invite of state.invites.values()) {
    const member = invite.acceptedSeq === null ? undefined : joinedBy.get(invite.acceptedSeq);
    if (member !== undefined) give(member, invitations.get(invite.inviteId), 'invitation');
  }
  const agents = new Map(
    [...state.principals.values()].filter((p) => p.kind === AGENT).map((p) => [p.id, p.parent!] as const),
  );
  return { orgName, workgroups, members, ownNames, sources, invitations, agents };
}

const NO_STATE = {};
const directories = new WeakMap<Profiles, WeakMap<object, Directory>>();

function cachedDirectory(profiles: Profiles, state: OrgState | null): Directory {
  let byState = directories.get(profiles);
  if (byState === undefined) {
    byState = new WeakMap();
    directories.set(profiles, byState);
  }
  const key = state ?? NO_STATE;
  if (!byState.has(key)) byState.set(key, directoryOf(profiles, state));
  return byState.get(key)!;
}

/** The names of an org from its org scope and its verified log (empty until the profiles were read). */
export const selectDirectory =
  (org: string) =>
  (state: StoreState): Directory => {
    const scope = state.events[org];
    if (scope === undefined) return EMPTY_DIRECTORY;
    return cachedDirectory(profilesOf(scope), orgEntry(state.orgs, org).log?.state ?? null);
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

/** The newest title index of each scope (weakly: decrypted titles do not outlive the store's events; its titles are kept
 * when unchanged, `sameAs`). */
const previousTitles = new Map<string, WeakRef<TitleIndex>>();

const titlesOf = memoize((scope: ScopeEvents): TitleIndex => {
  const previous = previousTitles.get(scopeIdOf(scope) ?? '')?.deref();
  const keep = (id: string, title: Title): Title => sameAs(previous?.byEvent.get(id)?.title, title);
  const byEvent = new Map<string, { task: string | null; title: Title }>();
  const byTask = new Map<string, Title>();
  const authors = new Map<string, string[]>();
  const entries = [...scope.entries.values()].sort((a, b) => a.scopeSeq - b.scopeSeq);
  for (const entry of entries) {
    if (entry.status === 'unverifiable') {
      const id = isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header.id : null;
      if (typeof id === 'string') {
        byEvent.set(id, { task: null, title: keep(id, { status: 'unverifiable', code: entry.code! }) });
      }
      continue;
    }
    if (entry.status !== 'verified' || entry.header!.kind !== TITLE_KIND) continue;
    const title = keep(entry.header!.id as string, { status: 'verified', text: text(entry.body, 'title') ?? '' });
    byEvent.set(entry.header!.id as string, { task: entry.header!.task as string, title });
    byTask.set(entry.header!.task as string, title);
    const task = entry.header!.task as string;
    const known = authors.get(task);
    if (known === undefined) authors.set(task, [entry.header!.author as string]);
    else known.push(entry.header!.author as string);
  }
  const index = { byEvent, byTask, authors };
  const scopeId = scopeIdOf(scope);
  if (scopeId !== null) previousTitles.set(scopeId, new WeakRef(index));
  return index;
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
 * that a user or an agent of the verified org state wrote; null while none is known.
 */
export const selectOwner =
  (org: string, wg: string, task: string) =>
  (state: StoreState): string | null => {
    const scope = state.events[wg];
    const principals = orgEntry(state.orgs, org).log?.state.principals;
    if (scope === undefined || principals === undefined) return null;
    for (const author of titlesOf(scope).authors.get(task) ?? []) {
      const kind = principals.get(author)?.kind;
      if (kind !== DEVICE) return kind === USER || kind === AGENT ? author : null;
    }
    return null;
  };
