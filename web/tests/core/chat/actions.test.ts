// Chat actions against the scriptable client of the task world: loading task and chat events, sending prompts with
// a kept sealed event for retries, session control, closing the task; the session detail load.
import { describe, expect, it } from 'vitest';
import { CHAT_KINDS, closeTask, controlSession, loadChat, sendPrompt } from '../../../src/core/chat/actions';
import { HubError } from '../../../src/core/net/signedFetch';
import { keepPrompt, setLoad } from '../../../src/core/store/slices/chat';
import { loadSession } from '../../../src/core/store/slices/sessions';
import { sessionRow, taskRow, taskWorld } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const SESSION = '0190f000-0000-7000-8000-0000000000a1';

describe('loadChat', () => {
  it('loads the task with its sessions and pulls the chat events of the task once', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({
      task: taskRow(t),
      sessions: [{ session: sessionRow(t), transitions: [] }],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    const row = await loadChat(t.context, TASK);
    expect(row!.task_id).toBe(TASK);
    expect(t.store.getState().chat.loads[TASK]).toEqual({ status: 'ready', code: null });
    const pull = new URL(t.client.of(`GET /v1/scopes/${t.wg}/events`)[0]!.target, 'https://h');
    expect(pull.searchParams.get('task')).toBe(TASK);
    expect(pull.searchParams.get('kinds')).toBe(CHAT_KINDS.join(','));
    await loadChat(t.context, TASK);
    expect(t.client.of(`GET /v1/scopes/${t.wg}/events`)).toHaveLength(1);
  });

  it('records the code of a failed load', async () => {
    const t = await taskWorld();
    expect(await loadChat(t.context, TASK)).toBeNull();
    expect(t.store.getState().chat.loads[TASK]).toEqual({ status: 'error', code: 'not_found' });
  });

  it('changes the load state only when it differs', async () => {
    const t = await taskWorld();
    setLoad(t.store, TASK, 'loading');
    const state = t.store.getState().chat;
    setLoad(t.store, TASK, 'loading');
    expect(t.store.getState().chat).toBe(state);
    keepPrompt(t.store, TASK, null);
    expect(t.store.getState().chat).toBe(state);
  });
});

describe('sendPrompt', () => {
  it('sends a sealed prompt and forgets it after success', async () => {
    const t = await taskWorld();
    const bodies: unknown[] = [];
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, (body) => {
      bodies.push(body);
      return { scope_seq: 4, session_id: SESSION };
    });
    expect(await sendPrompt(t.context, taskRow(t), 'Next step')).toBe(SESSION);
    expect(t.store.getState().chat.kept).toEqual({});
    expect(t.ingested).toHaveLength(1);
    expect(bodies).toHaveLength(1);
  });

  it('re-sends the identical event after a retryable failure, a new one for another text or refusal', async () => {
    const t = await taskWorld();
    const events: unknown[] = [];
    let fail: HubError | null = new HubError('unavailable', 503);
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, (body) => {
      events.push((body as { event: unknown }).event);
      if (fail !== null) throw fail;
      return { scope_seq: 4, session_id: SESSION };
    });
    const row = taskRow(t);
    await expect(sendPrompt(t.context, row, 'Again')).rejects.toMatchObject({ code: 'unavailable' });
    expect(t.store.getState().chat.kept[TASK]!.text).toBe('Again');
    fail = new HubError('rate_limited', 429);
    await expect(sendPrompt(t.context, row, 'Again')).rejects.toMatchObject({ code: 'rate_limited' });
    expect(events[1]).toBe(events[0]);
    fail = new HubError('forbidden', 403);
    await expect(sendPrompt(t.context, row, 'Changed')).rejects.toMatchObject({ code: 'forbidden' });
    expect(events[2]).not.toBe(events[0]);
    expect(t.store.getState().chat.kept).toEqual({});
    fail = null;
    await sendPrompt(t.context, row, 'Changed');
    expect(events[3]).not.toBe(events[2]);
  });
});

describe('controls', () => {
  it('posts interrupt and stop, closes the task and stores the closed row', async () => {
    const t = await taskWorld();
    const bodies: unknown[] = [];
    t.client.on(`POST /v1/sessions/${SESSION}/control`, (body) => {
      bodies.push(body);
      return {};
    });
    await controlSession(t.context, SESSION, 'interrupt');
    await controlSession(t.context, SESSION, 'stop');
    expect(bodies).toEqual([{ action: 'interrupt' }, { action: 'stop' }]);
    const closed = taskRow(t, { state: 'closed', closed_at: '2026-11-21T12:00:05.000Z' });
    t.client.on(`POST /v1/tasks/${TASK}/close`, () => ({ task: closed }));
    await closeTask(t.context, TASK);
    expect(t.store.getState().tasks.tasks[TASK]!.state).toBe('closed');
    t.client.on(`POST /v1/tasks/${TASK}/close`, () => ({ task: null }));
    await expect(closeTask(t.context, TASK)).rejects.toMatchObject({ code: 'unavailable' });
  });
});

describe('loadSession', () => {
  it('stores the session, loads its task and keeps the child IDs', async () => {
    const t = await taskWorld();
    const transition = { seq: 1, state: 'working', reason: null, at: 'x', received_at: 'y' };
    t.client.on(`GET /v1/sessions/${SESSION}`, () => ({
      session: sessionRow(t),
      transitions: [transition, { bad: true }],
      children: ['c1', 3],
    }));
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task: taskRow(t), sessions: [] }));
    const row = await loadSession(t.context, SESSION);
    expect(row!.session_id).toBe(SESSION);
    expect(t.store.getState().sessions.details[SESSION]).toEqual({ status: 'ready', code: null, children: ['c1'] });
    expect(t.store.getState().tasks.sessions[SESSION]!.transitions).toEqual([transition]);
    expect(t.store.getState().tasks.tasks[TASK]).toBeDefined();
    // a second answer without lists keeps the session and has no children; an odd one fails
    t.client.on(`GET /v1/sessions/${SESSION}`, () => ({ session: sessionRow(t) }));
    await loadSession(t.context, SESSION);
    expect(t.store.getState().sessions.details[SESSION]!.children).toEqual([]);
    t.client.on(`GET /v1/sessions/${SESSION}`, () => ({ session: null }));
    expect(await loadSession(t.context, SESSION)).toBeNull();
    expect(t.store.getState().sessions.details[SESSION]).toEqual({
      status: 'error',
      code: 'unavailable',
      children: [],
    });
    expect(await loadSession(t.context, 'unknown')).toBeNull();
    expect(t.store.getState().sessions.details.unknown!.code).toBe('not_found');
  });

  it('writes no session after a sign-out during the request (KOR-11)', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/sessions/${SESSION}`, () => {
      t.store.reset();
      return { session: sessionRow(t), transitions: [], children: [] };
    });
    expect((await loadSession(t.context, SESSION))!.session_id).toBe(SESSION);
    expect(t.store.getState().tasks.sessions).toEqual({});
    expect(t.client.of(`GET /v1/tasks/${TASK}`)).toHaveLength(0);
  });
});
