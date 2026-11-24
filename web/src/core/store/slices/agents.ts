// Slice `agents` (06-web.md section 8.3, 11-agent-access.md section 4): the user's connected AI agents as
// `core/agents/list.ts` last loaded them from `GET /v1/me/agents` – state, times, the agent each one replaced, and name
// and computer opened from the sealed label (null when the label does not open: an unknown agent). The list is the
// hub's, never the `?agent=` hint of a pairing. A user has one main orchestrator at a time (concept idea 34): the
// newest confirmed or active agent (`mainOrchestrator`); confirming a new one replaces it.
import type { AgentLabel } from '../../crypto/agent';
import type { Slice, Store, StoreState } from '../store';

export const AGENT_STATES = ['confirmed', 'expired', 'active', 'revoked'] as const;
export type AgentState = (typeof AGENT_STATES)[number];
/** The states of an agent that still has (or is about to get) access. */
export const LIVE_STATES: readonly AgentState[] = ['confirmed', 'active'];

export interface AgentEntry {
  readonly agentId: string;
  readonly state: AgentState;
  readonly label: AgentLabel | null;
  readonly createdAt: string;
  readonly confirmedAt: string;
  readonly claimedAt: string | null;
  readonly lastUsedAt: string | null;
  readonly revokedAt: string | null;
  /** The ID of the main orchestrator this one replaced at its confirmation, or null. */
  readonly replaces: string | null;
  /** Paired from another computer (ADR-067); false when the hub does not say. */
  readonly remote: boolean;
}

export interface AgentsState {
  readonly entries: readonly AgentEntry[] | null;
  readonly status: 'idle' | 'loading' | 'ready' | 'error';
  readonly code: string | null;
}

declare module '../store' {
  interface StoreState {
    agents: AgentsState;
  }
}

export const slice: Slice<'agents'> = {
  name: 'agents',
  initial: () => ({ entries: null, status: 'idle', code: null }),
};

export function setAgentsLoading(store: Store): void {
  store.update('agents', (state) => ({ ...state, status: 'loading', code: null }));
}

export function setAgents(store: Store, entries: readonly AgentEntry[]): void {
  store.update('agents', () => ({ entries, status: 'ready', code: null }));
}

export function setAgentsError(store: Store, code: string): void {
  store.update('agents', (state) => ({ ...state, status: 'error', code }));
}

/** The name of one of the user's agents (from its opened label), null when unknown or not loaded. */
export function agentName(state: StoreState, agentId: string): string | null {
  return state.agents.entries?.find((entry) => entry.agentId === agentId)?.label?.name ?? null;
}

/** The user's main orchestrator: the newest confirmed or active entry other than `except` (a pairing), else null. */
export function mainOrchestrator(entries: readonly AgentEntry[] | null, except?: string): AgentEntry | null {
  return entries?.find((entry) => LIVE_STATES.includes(entry.state) && entry.agentId !== except) ?? null;
}

/** The main orchestrator when it runs on another computer (the shell's banner, ADR-067 section 4.4), else null. */
export function remoteOrchestrator(entries: readonly AgentEntry[] | null): AgentEntry | null {
  const main = mainOrchestrator(entries);
  return main?.remote === true ? main : null;
}
