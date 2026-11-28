// The counters of the situation view as filters of the task overview (06-web.md sections 4.0 and 5.1a): each counter
// names the list behind its number, narrowed as the hub's summary counts it (`agent_hub.attention.read.summary`):
// "Waiting for me" the decisions of the user's own tasks, "Stuck" and "Running" their whole categories, "Finished
// today" the records finished since local midnight. The overview keeps the chosen one in its address
// (`?situation=…`); it carries no search text, so nothing private reaches the hub on a reload.
import { formatTime } from '../encoding/time';
import { type Category, type GroupFilter, NO_FILTER } from '../store/slices/attention';
import { localMidnight } from './load';

/** The counters in their order (also the values of `?situation=`). */
export const SITUATIONS = ['mine', 'blocked', 'working', 'done'] as const;
export type Situation = (typeof SITUATIONS)[number];

export const isSituation = (value: unknown): value is Situation => SITUATIONS.includes(value as Situation);

/** The group of the situation view behind a counter. */
export interface SituationList {
  readonly category: Category;
  readonly filter: GroupFilter;
}

/** The list behind the counter `situation` for the signed-in `userId` at `now` (module comment). */
export function situationList(situation: Situation, userId: string, now: number): SituationList {
  if (situation === 'mine') return { category: 'needs_decision', filter: { owner: userId, after: null } };
  if (situation === 'done') return { category: 'done', filter: { owner: null, after: formatTime(localMidnight(now)) } };
  return { category: situation, filter: NO_FILTER };
}
