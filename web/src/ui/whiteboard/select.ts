// The one store selector of a board (ADR-063 section 1): only the whiteboard events of the board's scope. A workgroup
// scope also carries the chat, terminal and operations of its tasks; selecting the whole scope would re-project and
// re-render the board on every chat event. The selector keeps its last result while the scope object is unchanged,
// and while the whiteboard events in it are the same objects, so the board renders only when one of them changes.
import type { Selector } from '../../core/store';
import { type EventEntry, kindOf, type ScopeEvents } from '../../core/store/slices/events';
import { WHITEBOARD_KINDS } from '../../core/whiteboards/project';

const KINDS: ReadonlySet<unknown> = new Set(WHITEBOARD_KINDS);

function sameEntries(a: ReadonlyMap<number, EventEntry>, b: ReadonlyMap<number, EventEntry>): boolean {
  if (a.size !== b.size) return false;
  for (const [seq, entry] of b) if (a.get(seq) !== entry) return false;
  return true;
}

/** A selector of the whiteboard events of `scopeId` (one per board: it keeps its own last result). */
export function selectBoardEvents(scopeId: string): Selector<ScopeEvents | undefined> {
  let source: ScopeEvents | undefined;
  let result: ScopeEvents | undefined;
  return (state) => {
    const scope = state.events[scopeId];
    if (scope === source) return result;
    source = scope;
    if (scope === undefined) {
      result = undefined;
      return result;
    }
    const entries = new Map<number, EventEntry>();
    for (const [seq, entry] of scope.entries) if (KINDS.has(kindOf(entry))) entries.set(seq, entry);
    if (result === undefined || !sameEntries(result.entries, entries)) result = { entries };
    return result;
  };
}
