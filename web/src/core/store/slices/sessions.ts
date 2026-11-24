// Slice `sessions` (06-web.md sections 5.3 and 5.5): the load state of the session detail (`GET /v1/sessions/{id}`).
// The session rows and transitions themselves live in the `tasks` slice (one source of session state, decision
// WEB-04 19); this slice adds what only the detail answers: the IDs of the children (the hub lists at most ten).
// `loadSession` stores the row, then loads the session's task (title, sibling and child rows).
import { HubError } from '../../net/signedFetch';
import { isRecord } from '../../registry';
import type { TasksContext } from '../../tasks/pull';
import { loadTask, sendErrorCode } from '../../tasks/send';
import type { Slice, Store } from '../store';
import { isSessionRow, isTransition, putSession, type SessionRow } from './tasks';

export interface SessionDetail {
  readonly status: 'loading' | 'ready' | 'error';
  readonly code: string | null;
  readonly children: readonly string[];
}

export interface SessionsState {
  readonly details: Readonly<Record<string, SessionDetail>>;
}

declare module '../store' {
  interface StoreState {
    sessions: SessionsState;
  }
}

export const slice: Slice<'sessions'> = { name: 'sessions', initial: () => ({ details: {} }) };

function setDetail(store: Store, session: string, detail: SessionDetail): void {
  store.update('sessions', (state) => ({ details: { ...state.details, [session]: detail } }));
}

/** Loads a session and its task; returns the row, or null with the code in the slice. */
export async function loadSession(context: TasksContext, session: string): Promise<SessionRow | null> {
  const { store } = context;
  const children = store.getState().sessions.details[session]?.children ?? [];
  setDetail(store, session, { status: 'loading', code: null, children });
  try {
    const generation = store.generation;
    const body = await context.client.request('GET', `/v1/sessions/${encodeURIComponent(session)}`);
    if (!isRecord(body) || !isSessionRow(body.session)) throw new HubError('unavailable', 200);
    // an answer after a sign-out writes nothing into the next user's store (KOR-11)
    if (store.generation !== generation) return body.session;
    const transitions = Array.isArray(body.transitions) ? body.transitions.filter(isTransition) : [];
    putSession(store, body.session, transitions, context.now());
    await loadTask(context, body.session.task_id);
    const ids = Array.isArray(body.children) ? body.children.filter((id) => typeof id === 'string') : [];
    setDetail(store, session, { status: 'ready', code: null, children: ids });
    return body.session;
  } catch (error) {
    setDetail(store, session, { status: 'error', code: sendErrorCode(error), children });
    return null;
  }
}
