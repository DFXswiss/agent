// Who is DFX support, and how a support person is named (ADR-096 sections 1 and 5).
//
// - The roster (`PlatformState.supportRoster`: active role, valid bound certification) and the support authors
//   (`supportAuthors`: everybody ever bound, also after leaving) come only from the verified platform log of this
//   browser; they are computed once per verified platform state.
// - A support person is shown as "Support <Name> #XXXX": the display name the operator set (`GET /v1/orgs/{org}/support`,
//   current persons only; a former person has none) next to the ID mark of the user (`senderMark`), so that a name the
//   hub changes cannot hide which certified person acted. The marks are computed once per user into the store.
import { senderMark } from '../community/mark';
import type { PlatformState } from '../log/platform';
import { NO_ROSTER, type Roster } from '../log/state';
import { setMarks } from '../store/slices/support';
import type { Store, StoreState } from '../store/store';

const rosters = new WeakMap<PlatformState, Roster>();
const authors = new WeakMap<PlatformState, Roster>();

/** The support roster of the verified platform state (empty without one). */
export function rosterOf(platform: PlatformState | null): Roster {
  if (platform === null) return NO_ROSTER;
  let found = rosters.get(platform);
  if (found === undefined) {
    found = platform.supportRoster();
    rosters.set(platform, found);
  }
  return found;
}

/** Every person ever bound to the role `support` (the authors of support events stay verifiable). */
export function authorsOf(platform: PlatformState | null): Roster {
  if (platform === null) return NO_ROSTER;
  let found = authors.get(platform);
  if (found === undefined) {
    found = platform.supportAuthors();
    authors.set(platform, found);
  }
  return found;
}

const platformOf = (state: StoreState): PlatformState | null => state.orgs.platform?.state ?? null;

/** The signed-in user is a person of the roster of its verified platform log (the support area is shown). */
export function isSupportPerson(state: StoreState): boolean {
  const user = state.me.userId;
  return user !== null && rosterOf(platformOf(state)).has(user);
}

/** A support author as the organization `org` names it: the operator's display name (null if none) and the mark. */
export interface SupportName {
  readonly name: string | null;
  /** `#XXXX`, null until computed (`ensureMarks`). */
  readonly mark: string | null;
}

/** The support name of `id`, or null if `id` was never a support person in the verified platform log. */
export function supportName(state: StoreState, org: string, id: string): SupportName | null {
  if (!authorsOf(platformOf(state)).has(id)) return null;
  return { name: state.support.orgs[org]?.people[id] ?? null, mark: state.support.marks[id] ?? null };
}

/** Computes the marks of `ids` that the store does not hold yet. */
export async function ensureMarks(store: Store, ids: Iterable<string>): Promise<void> {
  const known = store.getState().support.marks;
  const missing = [...new Set(ids)].filter((id) => known[id] === undefined);
  if (missing.length === 0) return;
  const generation = store.generation;
  const marks = Object.fromEntries(await Promise.all(missing.map(async (id) => [id, await senderMark(id)] as const)));
  if (store.generation === generation) setMarks(store, marks);
}
