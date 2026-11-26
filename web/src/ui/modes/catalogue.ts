// The catalogue of working modes for a view (`core/modes/modes.ts`, ADR-098 section 1): loaded once it is needed – a
// view that shows a task's modes or offers a choice; a hub without modes (older than KOR-75) is asked only then.
// `again`: ask once more after a failed load (the task page and the task form; the rows of a list do not).
import { useEffect } from 'react';
import { loadModes } from '../../core/modes/modes';
import type { ModesState } from '../../core/store/slices/modes';
import { useStore } from '../hooks/useStore';
import { useAppEnv } from '../shell/context';

export function useModeCatalogue(needed: boolean, again: boolean): ModesState {
  const { app } = useAppEnv();
  const state = useStore((store) => store.modes, app.store);
  useEffect(() => {
    if (needed) void loadModes(app.tasks, again);
  }, [app, needed, again]);
  return state;
}
