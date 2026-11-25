// The actions and selectors of the slice `pulls` (`core/store/slices/pulls.ts`, KOR-48): keys of links, lists and
// statuses, the list load states and pages, a new or removed link in every loaded list, and the statuses of pull
// requests (a status that is not `ready` keeps the last checked state). Kept apart from the slice registration so
// that they load with the pull-request code, not with the first screen (06-web.md section 12.2).
import '../i18n/lazy/pulls';
import type { LinkList, PullLink, PullsState, PullStatus } from '../store/slices/pulls';
import type { Store } from '../store/store';
import type { PullState } from './checks';

/** The newest checked state of a status (its own, or the one it kept), else null. */
export function lastState(status: PullStatus | null): PullState | null {
  if (status === null) return null;
  return status.status === 'ready' ? status.state : (status.last ?? null);
}

export type { LinkList, LinkListStatus, PullLink, PullsState, PullStatus } from '../store/slices/pulls';

export const linkId = (wg: string, epoch: number, tag: string): string => `${wg} ${epoch} ${tag}`;
export const listKey = (wg: string, task: string | null): string => `${wg} ${task ?? ''}`;
/** `wg` and the pull request's key (`core/pulls/address.ts` `pullKey`). */
export const statusKey = (wg: string, key: string): string => `${wg} ${key}`;

export const idOf = (link: PullLink): string => linkId(link.wg, link.epoch, link.tag);

const EMPTY: LinkList = { ids: [], nextCursor: null, status: 'loading', code: null };

/** The list of `key` (an empty loading list while none was asked for). */
export function linkList(state: PullsState, key: string): LinkList {
  return state.lists[key] ?? EMPTY;
}

export function listLoading(store: Store, key: string): void {
  store.update('pulls', (state) => ({
    ...state,
    lists: { ...state.lists, [key]: { ...linkList(state, key), status: 'loading', code: null } },
  }));
}

export function listFailed(store: Store, key: string, code: string): void {
  store.update('pulls', (state) => ({
    ...state,
    lists: { ...state.lists, [key]: { ...linkList(state, key), status: 'error', code } },
  }));
}

/** A page of a list: the links enter `links`; the first page replaces the IDs, a further one appends. */
export function listPage(
  store: Store,
  key: string,
  items: readonly PullLink[],
  nextCursor: string | null,
  first: boolean,
): void {
  store.update('pulls', (state) => {
    const links = { ...state.links };
    for (const item of items) links[idOf(item)] = item;
    const before = first ? [] : linkList(state, key).ids;
    const ids = [...new Set([...before, ...items.map(idOf)])];
    return { ...state, links, lists: { ...state.lists, [key]: { ids, nextCursor, status: 'ready', code: null } } };
  });
}

/** A new link enters every loaded list it belongs to (the workgroup's and its task's) at the top. */
export function addLink(store: Store, link: PullLink): void {
  const id = idOf(link);
  store.update('pulls', (state) => {
    const lists = { ...state.lists };
    for (const key of [listKey(link.wg, null), listKey(link.wg, link.taskId)]) {
      const list = state.lists[key];
      if (list !== undefined) lists[key] = { ...list, ids: [id, ...list.ids.filter((other) => other !== id)] };
    }
    return { ...state, links: { ...state.links, [id]: link }, lists };
  });
}

/** A removed link leaves every list. */
export function removeLink(store: Store, link: PullLink): void {
  const id = idOf(link);
  store.update('pulls', (state) => {
    const links = { ...state.links };
    delete links[id];
    const lists = Object.fromEntries(
      Object.entries(state.lists).map(([key, list]) => [
        key,
        { ...list, ids: list.ids.filter((other) => other !== id) },
      ]),
    );
    return { ...state, links, lists };
  });
}

/** Sets the status of several pull requests of a workgroup (`keys`: `pullKey`s). */
export function setStatuses(store: Store, wg: string, entries: readonly (readonly [string, PullStatus])[]): void {
  if (entries.length === 0) return;
  store.update('pulls', (state) => {
    const status = { ...state.status };
    for (const [key, value] of entries) {
      const id = statusKey(wg, key);
      const last = lastState(status[id] ?? null);
      status[id] = value.status === 'ready' || last === null ? value : { ...value, last };
    }
    return { ...state, status };
  });
}

const NONE: PullStatus | null = null;

/** The status of a pull request of a workgroup, null while it was never asked for. */
export function pullStatus(state: PullsState, wg: string, key: string): PullStatus | null {
  return state.status[statusKey(wg, key)] ?? NONE;
}
