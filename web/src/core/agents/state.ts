// Reading and changing the slice `mainAgents` (06-web.md section 4.0a, ADR-095 section 5): the person rows of an org
// and the main agents of a person as the hub paged them, and the rows the user has open. Kept apart from the slice's
// registration (`store/slices/mainAgents.ts`, part of the first load): only the situation view by person loads this.
import type { AttentionRow } from '../store/slices/attention';
import type { AgentList, MainAgentsState, PeopleList, PersonRow } from '../store/slices/mainAgents';
import type { Store } from '../store/store';

export const agentsKey = (org: string, owner: string): string => `${org} ${owner}`;

const IDLE_PEOPLE: PeopleList = { owners: [], rows: {}, nextAfter: null, status: 'idle', code: null };
const IDLE_AGENTS: AgentList = { ids: [], nextBefore: null, status: 'idle', code: null };

/** The person rows of `org` (an idle empty list when not loaded yet, the same object every time). */
export const peopleList = (state: MainAgentsState, org: string): PeopleList => state.people[org] ?? IDLE_PEOPLE;

/** The main agents of `owner` in `org` (an idle empty list when not loaded yet, the same object every time). */
export const agentList = (state: MainAgentsState, org: string, owner: string): AgentList =>
  state.lists[agentsKey(org, owner)] ?? IDLE_AGENTS;

function setPeople(store: Store, org: string, change: (list: PeopleList) => PeopleList): void {
  store.update('mainAgents', (state) => ({
    ...state,
    people: { ...state.people, [org]: change(peopleList(state, org)) },
  }));
}

function setAgents(store: Store, org: string, owner: string, change: (list: AgentList) => AgentList): void {
  store.update('mainAgents', (state) => ({
    ...state,
    lists: { ...state.lists, [agentsKey(org, owner)]: change(agentList(state, org, owner)) },
  }));
}

/** Whether the user has the row of `owner` in `org` open. */
export const personOpen = (state: MainAgentsState, org: string, owner: string): boolean =>
  state.open[agentsKey(org, owner)] === true;

/** Opens or closes the row of `owner` in `org`. */
export function setPersonOpen(store: Store, org: string, owner: string, open: boolean): void {
  store.update('mainAgents', (state) => {
    const { [agentsKey(org, owner)]: _was, ...rest } = state.open;
    return { ...state, open: open ? { ...rest, [agentsKey(org, owner)]: true } : rest };
  });
}

export function peopleLoading(store: Store, org: string): void {
  setPeople(store, org, (list) => ({ ...list, status: 'loading', code: null }));
}

export function peopleFailed(store: Store, org: string, code: string): void {
  setPeople(store, org, (list) => ({ ...list, status: 'error', code }));
}

/** A page of person rows (`first`: the list starts over with it; a person already listed is updated in place). */
export function peoplePage(
  store: Store,
  page: { org: string; rows: readonly PersonRow[]; nextAfter: string | null; first: boolean },
): void {
  setPeople(store, page.org, (list) => {
    const kept = page.first ? [] : list.owners;
    const owners = [...new Set([...kept, ...page.rows.map((row) => row.owner_id)])];
    const base = page.first ? {} : list.rows;
    const rows = { ...base, ...Object.fromEntries(page.rows.map((row) => [row.owner_id, row])) };
    return { owners, rows, nextAfter: page.nextAfter, status: 'ready', code: null };
  });
}

export function agentsLoading(store: Store, org: string, owner: string): void {
  setAgents(store, org, owner, (list) => ({ ...list, status: 'loading', code: null }));
}

export function agentsFailed(store: Store, org: string, owner: string, code: string): void {
  setAgents(store, org, owner, (list) => ({ ...list, status: 'error', code }));
}

/** A page of a person's main agents with the attention records it brought (`first`: the list starts over). */
export function agentsPage(
  store: Store,
  page: {
    org: string;
    owner: string;
    ids: readonly string[];
    attention: readonly AttentionRow[];
    nextBefore: string | null;
    first: boolean;
  },
): void {
  store.update('mainAgents', (state) => {
    const list = agentList(state, page.org, page.owner);
    const ids = [...new Set([...(page.first ? [] : list.ids), ...page.ids])];
    const updated: AgentList = { ids, nextBefore: page.nextBefore, status: 'ready', code: null };
    const attention = { ...state.attention, ...Object.fromEntries(page.attention.map((row) => [row.task_id, row])) };
    return { ...state, lists: { ...state.lists, [agentsKey(page.org, page.owner)]: updated }, attention };
  });
}
