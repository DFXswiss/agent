// U-WEB-ui-tasks (captured tasks, E-41, ADR-100): the hub fields of a row (also from an older hub), the sealed fields
// from the newest `task.meta` carrying each, the form checks, capture (`POST /v1/tasks` with `task.meta` only) and its
// answer checks, the field changes (`PUT …/fields`), a new `task.meta` with the full set, "Start agent", the people of
// the org and the topics (`topic.profile`: read, pulled, created).
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openEvent } from '../../../src/core/crypto/event';
import { epochKey, ORG, type OrgState, WG } from '../../../src/core/log/state';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { blockOrg } from '../../../src/core/store/slices/orgs';
import type { TaskRow } from '../../../src/core/store/slices/tasks';
import { SendError, sendErrorCode } from '../../../src/core/tasks/send';
import {
  assignablePeople,
  changedFields,
  createTopic,
  EMPTY_META,
  loadTopics,
  metaBody,
  metaChanged,
  prepareCapture,
  putFields,
  selectMeta,
  selectTopics,
  sendCapture,
  startAgent,
  startPrompt,
  taskAssignee,
  taskStatus,
  taskTopic,
  taskUrgency,
  topicName,
  writeMeta,
} from '../../../src/core/tasks/tickets';
import { actor } from '../log/world';
import { taskRow, type TaskWorld, taskWorld } from './world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const TOPIC = '0190f000-0000-7000-8000-0000000000c1';

async function rejects(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return sendErrorCode(error);
  }
  throw new Error('expected a rejection');
}

const row = (t: TaskWorld, overrides: Record<string, unknown> = {}) =>
  taskRow(t, { state: 'idle', main_session_id: null, ...overrides }) as TaskRow;

function entry(
  t: TaskWorld,
  scope: string,
  seq: number,
  header: Record<string, unknown>,
  body: unknown,
  status: EventEntry['status'] = 'verified',
): EventEntry {
  return {
    org: t.org,
    scopeId: scope,
    scopeSeq: seq,
    receivedAt: null,
    event: { header: { id: `e${seq}` } },
    status,
    code: status === 'verified' ? null : 'signature_invalid',
    header: status === 'verified' ? { id: `e${seq}`, author: t.me.id, ...header } : null,
    body: status === 'verified' ? body : null,
  };
}

const INPUT = { title: '  Look at invoices ', description: ' Before Friday ', githubOrg: 'DFXswiss', githubRepo: '' };

describe('the hub fields of a row', () => {
  it('reads the hub fields, and derives them for an older hub', async () => {
    const t = await taskWorld();
    const full = row(t, { status: 'todo', urgency: 'high', assignee_id: t.ada.id, topic_id: TOPIC });
    expect([taskStatus(full), taskUrgency(full), taskAssignee(full), taskTopic(full)]).toEqual([
      'todo',
      'high',
      t.ada.id,
      TOPIC,
    ]);
    const old = row(t, { state: 'working', urgency: 'whatever' });
    expect([taskStatus(old), taskUrgency(old), taskAssignee(old), taskTopic(old)]).toEqual([
      'inProgress',
      'normal',
      null,
      null,
    ]);
    expect(taskStatus(row(t, { state: 'closed', status: 'bogus' }))).toBe('completed');
  });

  it('lists only the changed hub fields', async () => {
    const t = await taskWorld();
    const base = row(t, { status: 'todo', urgency: 'normal', assignee_id: null, topic_id: TOPIC });
    const same = { status: 'todo', urgency: 'normal', assignee_id: null, topic_id: TOPIC } as const;
    expect(changedFields(base, same)).toEqual({});
    expect(
      changedFields(base, { status: 'completed', urgency: 'urgent', assignee_id: t.ada.id, topic_id: null }),
    ).toEqual({ status: 'completed', urgency: 'urgent', assignee_id: t.ada.id, topic_id: null });
  });
});

describe('the sealed fields', () => {
  it('takes each field from the newest verified task.meta that carries it', async () => {
    const t = await taskWorld();
    expect(selectMeta(t.wg, TASK)(t.store.getState())).toBeNull();
    putEvents(t.store, t.wg, [
      entry(t, t.wg, 1, { kind: 'task.meta', task: TASK }, { title: 'First', description: 'Desc', github_org: 'acme' }),
      entry(t, t.wg, 2, { kind: 'prompt', task: TASK }, { text: 'x' }),
      // a device's task.set_title writes only the title
      entry(t, t.wg, 3, { kind: 'task.meta', task: TASK }, { title: 'Renamed', github_repo: 7 }),
      entry(t, t.wg, 4, { kind: 'task.meta', task: TASK }, null, 'unverifiable'),
      entry(t, t.wg, 5, { kind: 'task.meta', task: 'other' }, 'not a body'),
    ]);
    expect(selectMeta(t.wg, TASK)(t.store.getState())).toEqual({
      title: 'Renamed',
      description: 'Desc',
      github_org: 'acme',
      github_repo: '',
    });
    expect(selectMeta(t.wg, 'other')(t.store.getState())).toEqual(EMPTY_META);
    expect(selectMeta(t.wg, 'unknown')(t.store.getState())).toBeNull();
  });

  it('checks and trims the form input', () => {
    expect(metaBody(INPUT)).toEqual({
      title: 'Look at invoices',
      description: 'Before Friday',
      github_org: 'DFXswiss',
      github_repo: '',
    });
    expect(metaBody({ ...INPUT, githubRepo: 'dfxswiss/agent' }).github_repo).toBe('dfxswiss/agent');
    expect(metaBody({ ...INPUT, githubOrg: '', githubRepo: 'acme/web' }).github_repo).toBe('acme/web');
    const invalid = [
      { title: '  ' },
      { title: 'x'.repeat(121) },
      { title: 'two\nlines' },
      { description: 'x'.repeat(4097) },
      { githubOrg: '-bad' },
      { githubRepo: 'no-slash' },
      { githubRepo: 'other/agent' },
    ];
    for (const change of invalid) expect(() => metaBody({ ...INPUT, ...change })).toThrow(SendError);
  });

  it('compares sealed fields and builds the start prompt', () => {
    expect(metaChanged(EMPTY_META, { ...EMPTY_META })).toBe(false);
    expect(metaChanged(EMPTY_META, { ...EMPTY_META, github_repo: 'a/b' })).toBe(true);
    expect(startPrompt({ title: 'Title', description: '  ' })).toBe('Title');
    expect(startPrompt({ title: 'Title', description: ' Text \n' })).toBe('Title\n\nText');
  });
});

describe('capture', () => {
  it('seals the task.meta only, with the optional hub fields', async () => {
    const t = await taskWorld();
    const prepared = await prepareCapture(t.context, {
      org: t.org,
      wg: t.wg,
      meta: INPUT,
      urgency: 'high',
      assigneeId: t.ada.id,
      topicId: TOPIC,
    });
    expect(prepared.body).toMatchObject({
      task_id: prepared.taskId,
      wg_id: t.wg,
      urgency: 'high',
      assignee_id: t.ada.id,
      topic_id: TOPIC,
    });
    expect(prepared.body.events).toHaveLength(1);
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const event = prepared.body.events[0] as { header: Record<string, unknown> };
    expect(event.header).toMatchObject({ kind: 'task.meta', task: prepared.taskId, scope_id: t.wg });
    const sig = b64uDecode(t.me.identity.public().sig);
    // a capture leaves the empty fields out
    expect(JSON.parse(fromUtf8(await openEvent(key, event, sig)))).toEqual({
      title: 'Look at invoices',
      description: 'Before Friday',
      github_org: 'DFXswiss',
    });
    const plain = await prepareCapture(t.context, { org: t.org, wg: t.wg, meta: INPUT, assigneeId: null });
    expect(Object.keys(plain.body).sort()).toEqual(['events', 'task_id', 'wg_id']);
  });

  it('refuses without rights, a usable log or valid input', async () => {
    const t = await taskWorld();
    const input = { org: t.org, wg: t.wg, meta: INPUT };
    expect(await rejects(prepareCapture(t.context, { ...input, wg: 'unknown' }))).toBe('forbidden');
    expect(await rejects(prepareCapture(t.context, { ...input, urgency: 'soon' as never }))).toBe('request_invalid');
    expect(await rejects(prepareCapture(t.context, { ...input, meta: { ...INPUT, title: '' } }))).toBe(
      'request_invalid',
    );
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    expect(await rejects(prepareCapture(t.context, input))).toBe('log_empty');
    blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: 3 });
    expect(await rejects(prepareCapture(t.context, input))).toBe('log_fork');
    t.store.update('me', (me) => ({ ...me, userId: null }));
    expect(await rejects(prepareCapture(t.context, input))).toBe('unauthorized');
  });

  it('sends the capture, stores the row and reads the own event', async () => {
    const t = await taskWorld();
    const prepared = await prepareCapture(t.context, { org: t.org, wg: t.wg, meta: INPUT });
    const answer = (session: unknown, events: unknown, task = row(t, { task_id: prepared.taskId })) => {
      t.client.on('POST /v1/tasks', () => ({ task, session, events }));
    };
    answer(null, [{ id: 'x', scope_id: t.wg, scope_seq: 7 }, { id: 'y', scope_id: t.wg, scope_seq: 8 }]);
    const sent = await sendCapture(t.context, prepared);
    expect(sent.task_id).toBe(prepared.taskId);
    expect(t.store.getState().tasks.tasks[prepared.taskId]).toEqual(sent);
    expect(t.ingested).toEqual([
      { org: t.org, scope: t.wg, items: [{ scope_seq: 7, received_at: null, event: prepared.body.events[0] }] },
    ]);
    expect(t.client.of('POST /v1/tasks')[0]!.body).toBe(prepared.body);
    // answers without usable event results are stored without reading
    answer(null, 'none');
    await sendCapture(t.context, prepared);
    answer(null, [null, { id: 'x', scope_id: 'other', scope_seq: 1 }]);
    await sendCapture(t.context, prepared);
    expect(t.ingested).toHaveLength(1);
    // malformed answers
    answer({ session_id: 's' }, []);
    expect(await rejects(sendCapture(t.context, prepared))).toBe('unavailable');
    answer(null, [], row(t, { task_id: 'another' }));
    expect(await rejects(sendCapture(t.context, prepared))).toBe('unavailable');
    t.client.on('POST /v1/tasks', () => ({ task: { task_id: prepared.taskId }, session: null }));
    expect(await rejects(sendCapture(t.context, prepared))).toBe('unavailable');
    t.client.on('POST /v1/tasks', () => 'nothing');
    expect(await rejects(sendCapture(t.context, prepared))).toBe('unavailable');
  });
});

describe('edit', () => {
  it('puts the changed hub fields and stores the answer', async () => {
    const t = await taskWorld();
    const task = row(t, { status: 'todo' });
    expect(await putFields(t.context, task, {})).toBe(task);
    expect(t.client.calls).toEqual([]);
    const changed = row(t, { status: 'completed', state: 'closed', state_at: '2026-11-21T12:00:01.000Z' });
    t.client.on(`PUT /v1/tasks/${TASK}/fields`, () => ({ task: changed }));
    expect(await putFields(t.context, task, { status: 'completed', assignee_id: null })).toEqual(changed);
    expect(t.client.of(`PUT /v1/tasks/${TASK}/fields`)[0]!.body).toEqual({ status: 'completed', assignee_id: null });
    expect(t.store.getState().tasks.tasks[TASK]).toEqual(changed);
    for (const bad of ['x', { task: { task_id: TASK } }, { task: row(t, { task_id: 'other' }) }]) {
      t.client.on(`PUT /v1/tasks/${TASK}/fields`, () => bad);
      expect(await rejects(putFields(t.context, task, { urgency: 'low' }))).toBe('unavailable');
    }
  });

  it('writes the full set of the sealed fields as a new task.meta and reloads the row', async () => {
    const t = await taskWorld();
    const task = row(t);
    t.client
      .on('POST /v1/events', () => ({ results: [{ id: 'm', scope_id: t.wg, scope_seq: 12 }] }))
      .on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
    expect(await writeMeta(t.context, task, { ...INPUT, githubOrg: '' })).toBe(12);
    const event = (t.client.of('POST /v1/events')[0]!.body as { events: { event: unknown }[] }).events[0]!.event;
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    // an edit writes every field, "" clears
    expect(JSON.parse(fromUtf8(await openEvent(key, event, b64uDecode(t.me.identity.public().sig))))).toEqual({
      title: 'Look at invoices',
      description: 'Before Friday',
      github_org: '',
      github_repo: '',
    });
    expect(t.ingested.at(-1)).toEqual({
      org: t.org,
      scope: t.wg,
      items: [{ scope_seq: 12, received_at: null, event }],
    });
    expect(t.client.of(`GET /v1/tasks/${TASK}`)).toHaveLength(1);
    // a failed reload is not an error of the write
    t.client.on(`GET /v1/tasks/${TASK}`, () => 'broken');
    expect(await writeMeta(t.context, task, INPUT)).toBe(12);
    for (const bad of ['x', { results: [] }, { results: [{ scope_id: 'other', scope_seq: 1 }] }, { results: 'x' }]) {
      t.client.on('POST /v1/events', () => bad);
      expect(await rejects(writeMeta(t.context, task, INPUT))).toBe('unavailable');
    }
    expect(await rejects(writeMeta(t.context, row(t, { wg_id: 'unknown' }), INPUT))).toBe('forbidden');
  });

  it('starts the agent with the first prompt and reloads the row', async () => {
    const t = await taskWorld();
    const task = row(t);
    t.client
      .on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }))
      .on(`POST /v1/tasks/${TASK}/prompts`, () => ({ session_id: 's1', scope_seq: 4 }))
      .on(`GET /v1/tasks/${TASK}`, () => ({ task: { ...task, main_session_id: 's1' }, sessions: [] }));
    expect(await startAgent(t.context, task, 'Title\n\nText')).toBe('s1');
    expect(t.client.calls.map((call) => `${call.method} ${call.target}`).slice(0, 2)).toEqual([
      `GET /v1/workgroups/${t.wg}`,
      `POST /v1/tasks/${TASK}/prompts`,
    ]);
    expect(t.store.getState().tasks.tasks[TASK]!.main_session_id).toBe('s1');
    expect(t.store.getState().tasks.launches[TASK]!.sessionId).toBe('s1');
    t.client.on(`GET /v1/tasks/${TASK}`, () => 'broken');
    expect(await startAgent(t.context, task, 'again')).toBe('s1');
    expect(await rejects(startAgent(t.context, task, '   '))).toBe('request_invalid');
  });
});

describe('people', () => {
  it('lists the active users first, then the agents', async () => {
    const t = await taskWorld();
    const [carl, dora] = [await actor('carl'), await actor('dora')];
    await t.world.admitUser(t.ada, carl, 'member');
    await t.world.admitUser(t.ada, dora, 'member');
    const people = assignablePeople(t.world.state);
    expect(people.slice(0, 3).sort()).toEqual([t.ada.id, carl.id, dora.id].sort());
    expect(people).toEqual([...people].sort((a, b) => Number(a > b) - Number(a < b)));
    // an agent account sorts after the users (ADR-071)
    const state = t.world.state;
    const agent = { ...state.principals.get(carl.id)!, id: '0000-agent', kind: 'agent', parent: carl.id };
    const removed = { ...state.principals.get(dora.id)!, id: '0000-removed', removedSeq: 3 };
    const principals = new Map([...state.principals, [agent.id, agent], [removed.id, removed]]);
    expect(assignablePeople({ principals } as unknown as OrgState)).toEqual([...people, agent.id]);
  });
});

describe('topics', () => {
  it('names each topic by its newest verified topic.profile, sorted by name', async () => {
    const t = await taskWorld();
    expect(selectTopics(t.org)(t.store.getState())).toEqual([]);
    const A = '0190f000-0000-7000-8000-0000000000c2';
    const B = '0190f000-0000-7000-8000-0000000000c3';
    putEvents(t.store, t.org, [
      entry(t, t.org, 1, { kind: 'topic.profile', ref: TOPIC }, { name: 'Billing' }),
      entry(t, t.org, 2, { kind: 'topic.profile', ref: A }, { name: 'Accounting' }),
      entry(t, t.org, 3, { kind: 'topic.profile', ref: TOPIC }, { name: 'Invoices' }),
      entry(t, t.org, 4, { kind: 'topic.profile', ref: B }, { name: 'Invoices' }),
      entry(t, t.org, 5, { kind: 'topic.profile', ref: A }, 'broken'),
      entry(t, t.org, 6, { kind: 'topic.profile', ref: A }, { name: 5 }),
      entry(t, t.org, 7, { kind: 'org.profile', ref: '' }, { name: 'Org' }),
      entry(t, t.org, 8, { kind: 'topic.profile', ref: B }, null, 'unverifiable'),
    ]);
    expect(selectTopics(t.org)(t.store.getState())).toEqual([
      { id: A, name: 'Accounting' },
      { id: TOPIC, name: 'Invoices' },
      { id: B, name: 'Invoices' },
    ]);
  });

  it('checks topic names', () => {
    expect(topicName('  Billing ')).toBe('Billing');
    expect(topicName(' ')).toBeNull();
    expect(topicName('x'.repeat(101))).toBeNull();
    expect(topicName('a\nb')).toBeNull();
  });

  it('pulls the topics of the org once', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_before: null }));
    await loadTopics(t.context, t.org);
    await loadTopics(t.context, t.org);
    const calls = t.client.of(`GET /v1/scopes/${t.org}/events`);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.target).toContain('kinds=topic.profile');
  });

  it('creates and renames a topic as any member, sealed under the org key', async () => {
    const t = await taskWorld({ member: true });
    t.client.on('POST /v1/events', () => ({ results: [{ id: 'x', scope_id: t.org, scope_seq: 9 }] }));
    const id = await createTopic(t.context, t.org, ' Invoices ');
    const event = (t.client.of('POST /v1/events')[0]!.body as { events: { event: unknown }[] }).events[0]!.event as {
      header: Record<string, unknown>;
    };
    expect(event.header).toMatchObject({
      kind: 'topic.profile',
      scope: ORG,
      scope_id: t.org,
      ref: id,
      wg: '',
      task: '',
      author: t.me.id,
    });
    const key = t.world.held.get(t.me.id)!.get(epochKey(ORG, t.org, t.world.state.epoch(ORG, t.org)))!;
    expect(JSON.parse(fromUtf8(await openEvent(key, event, b64uDecode(t.me.identity.public().sig))))).toEqual({
      name: 'Invoices',
    });
    expect(t.ingested.at(-1)!.items[0]!.scope_seq).toBe(9);
    expect(await createTopic(t.context, t.org, 'Renamed', TOPIC)).toBe(TOPIC);
  });

  it('refuses invalid names, missing rights or keys and malformed answers', async () => {
    const t = await taskWorld();
    expect(await rejects(createTopic(t.context, t.org, ''))).toBe('request_invalid');
    for (const bad of ['x', { results: [] }, { results: [{ scope_id: 'other', scope_seq: 1 }] }, { results: 1 }]) {
      t.client.on('POST /v1/events', () => bad);
      expect(await rejects(createTopic(t.context, t.org, 'Name'))).toBe('unavailable');
    }
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, keys: new Map() } },
    }));
    expect(await rejects(createTopic(t.context, t.org, 'Name'))).toBe('epoch_stale');
    t.store.update('me', (me) => ({ ...me, userId: '0190f000-0000-7000-8000-0000000000ff' }));
    expect(await rejects(createTopic(t.context, t.org, 'Name'))).toBe('forbidden');
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    expect(await rejects(createTopic(t.context, t.org, 'Name'))).toBe('log_empty');
    blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: 3 });
    expect(await rejects(createTopic(t.context, t.org, 'Name'))).toBe('log_fork');
    t.store.update('me', (me) => ({ ...me, identity: null }));
    expect(await rejects(createTopic(t.context, t.org, 'Name'))).toBe('unauthorized');
  });
});
