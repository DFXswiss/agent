// Slice `mainAgents` (06-web.md section 4.0a, ADR-095 section 5): the situation view by person – per org the person
// rows (`GET /v1/orgs/{org}/people/agents`, paged by the hub after `owner_id`: the caller first, then by `owner_id`)
// and per person the open main agents (`GET /v1/orgs/{org}/main-agents?owner=`, newest activity first, paged with
// `next_before`), and the attention records those pages brought. The task rows of a page go into the `tasks` slice
// (`core/agents/people.ts`); here are only the IDs in the hub's order, the cursor of the next page and the load state.
// Malformed rows are refused by the loader, never half-applied. Only the state and its registration live here (every
// slice is part of the first load); reading and changing it is `core/agents/state.ts`, loaded with the view.
import type { AttentionRow } from './attention';
import type { Slice } from '../store';

/** `agent_person` of the contracts: the counts of one person's open main agents in the readable workgroups. */
export interface PersonRow {
  readonly owner_id: string;
  readonly agents: number;
  readonly needs_decision: number;
  readonly blocked: number;
  readonly working: number;
  readonly subagents_active: number;
}

export type ListStatus = 'idle' | 'loading' | 'ready' | 'error';

/** The person rows of an org in the order the hub paged them. */
export interface PeopleList {
  readonly owners: readonly string[];
  readonly rows: Readonly<Record<string, PersonRow>>;
  /** The cursor of the next page (`next_after`), null at the end. */
  readonly nextAfter: string | null;
  readonly status: ListStatus;
  readonly code: string | null;
}

/** The open main agents of one person: task IDs in the hub's order and the cursor of the next page. */
export interface AgentList {
  readonly ids: readonly string[];
  readonly nextBefore: string | null;
  readonly status: ListStatus;
  readonly code: string | null;
}

export interface MainAgentsState {
  /** Org → its person rows. */
  readonly people: Readonly<Record<string, PeopleList>>;
  /** `agentsKey(org, owner)` → the person's main agents. */
  readonly lists: Readonly<Record<string, AgentList>>;
  /** Task ID → the attention record a page of main agents brought with it. */
  readonly attention: Readonly<Record<string, AttentionRow>>;
  /** `agentsKey(org, owner)` → true while the user has the person's row open (kept across refreshes and remounts). */
  readonly open: Readonly<Record<string, true>>;
}

declare module '../store' {
  interface StoreState {
    mainAgents: MainAgentsState;
  }
}

export const slice: Slice<'mainAgents'> = {
  name: 'mainAgents',
  initial: () => ({ people: {}, lists: {}, attention: {}, open: {} }),
};
