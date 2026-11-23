// Selectors of the base slices. Selectors that derive new arrays memoize on the identity of their input object, so a
// React component that calls `useStore(selector)` gets the same snapshot until the slice really changed.
import type { ConfigState } from './slices/config';
import type { ConnectionState } from './slices/connection';
import type { EventEntry, ScopeEvents } from './slices/events';
import type { JobsState } from './slices/jobs';
import type { MeState } from './slices/me';
import { type OrgEntry, orgEntry } from './slices/orgs';
import type { Selector } from './store';

/** Memoizes `derive` per input object (weakly held). */
export function memoize<I extends object, O>(derive: (input: I) => O): (input: I) => O {
  const cache = new WeakMap<I, O>();
  return (input) => {
    if (cache.has(input)) return cache.get(input)!;
    const output = derive(input);
    cache.set(input, output);
    return output;
  };
}

const NO_EVENTS: readonly EventEntry[] = [];
const ascending = memoize((scope: ScopeEvents): readonly EventEntry[] =>
  [...scope.entries.values()].sort((a, b) => a.scopeSeq - b.scopeSeq),
);

/** The events of a scope in ascending `scope_seq` (chat and logs are ascending, 06-web.md section 12.4). */
export const selectScopeEvents =
  (scopeId: string): Selector<readonly EventEntry[]> =>
  (state) => {
    const scope = state.events[scopeId];
    return scope === undefined ? NO_EVENTS : ascending(scope);
  };

/** One org's entry. */
export const selectOrg =
  (org: string): Selector<OrgEntry | null> =>
  (state) =>
    state.orgs.byId[org] ?? null;

/** The blocking error of an org, null while it is usable. */
export const selectOrgBlock =
  (org: string): Selector<OrgEntry['blocked']> =>
  (state) =>
    orgEntry(state.orgs, org).blocked;

export const selectConnection: Selector<ConnectionState> = (state) => state.connection;
export const selectJobs: Selector<JobsState> = (state) => state.jobs;
export const selectMe: Selector<MeState> = (state) => state.me;
export const selectConfig: Selector<ConfigState['config']> = (state) => state.config.config;
