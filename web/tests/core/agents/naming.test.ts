// KOR-52 (ADR-095 section 2, 06-web.md section 4.0a): the name of a main agent from the verified `task.agent_name`
// events of its task – given, automatic, unverifiable (never replaced by an older one), missing while the named event
// is not read – the shown text with the automatic name of the title, the pulls of missing names, and the rename (sealed
// under the workgroup key, rights, input checks, the hub's answer, read into the store at once).
import { describe, expect, it } from 'vitest';
import {
  AGENT_NAME_KIND,
  agentLabel,
  agentNames,
  canRename,
  renameAgent,
  selectAgentName,
} from '../../../src/core/agents/naming';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openCheckedEvent } from '../../../src/core/log/events';
import { epochKey, WG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { blockOrg } from '../../../src/core/store/slices/orgs';
import type { TaskRow } from '../../../src/core/store/slices/tasks';
import { sendErrorCode } from '../../../src/core/tasks/send';
import { taskRow, type TaskWorld, taskWorld } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const OTHER = '0190f000-0000-7000-8000-000000000002';

function named(
  t: TaskWorld,
  seq: number,
  body: unknown,
  options: { task?: string; status?: EventEntry['status']; kind?: string; id?: string } = {},
): EventEntry {
  const header = {
    id: options.id ?? `n${seq}`,
    kind: options.kind ?? AGENT_NAME_KIND,
    task: options.task ?? TASK,
    author: t.me.id,
  };
  const status = options.status ?? 'verified';
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: status === 'unverifiable' ? { header: { id: header.id } } : { header },
    status,
    code: status === 'unverifiable' ? 'signature_invalid' : null,
    header: status === 'unverifiable' ? null : header,
    body,
  };
}

const select = (t: TaskWorld, nameEventId: string | null = null, task = TASK) =>
  selectAgentName(t.wg, task, nameEventId)(t.store.getState());

async function rejects(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return sendErrorCode(error);
  }
  throw new Error('expected a rejection');
}

describe('selectAgentName', () => {
  it('is automatic without a name event and missing while the named one is not read', async () => {
    const t = await taskWorld();
    expect(select(t)).toEqual({ status: 'automatic' });
    expect(select(t, 'n9')).toEqual({ status: 'missing' });
    putEvents(t.store, t.wg, [named(t, 1, { name: 'Other' }, { task: OTHER })]);
    expect(select(t)).toEqual({ status: 'automatic' });
    expect(select(t, 'n9')).toEqual({ status: 'missing' });
  });

  it('takes the newest verified name of the task: given, automatic again, or a body that breaks the schema', async () => {
    const t = await taskWorld();
    putEvents(t.store, t.wg, [
      named(t, 2, { name: 'Login doctor' }),
      named(t, 1, { name: 'Older' }),
      named(t, 3, { name: 'Pending' }, { status: 'pending' }),
      named(t, 4, { title: 'x' }, { kind: 'task.meta' }),
      named(t, 5, { name: 'Elsewhere' }, { task: OTHER }),
    ]);
    expect(select(t)).toEqual({ status: 'given', text: 'Login doctor' });
    expect(select(t, 'n2')).toEqual({ status: 'given', text: 'Login doctor' });
    putEvents(t.store, t.wg, [named(t, 6, { name: null })]);
    expect(select(t)).toEqual({ status: 'automatic' });
    for (const [seq, body] of [
      [7, { name: 'two\nlines' }],
      [8, { name: '' }],
      [9, { name: 'a'.repeat(81) }],
      [10, { name: 7 }],
      [11, { name: 'x', extra: 1 }],
      [12, {}],
      [13, 'text'],
    ] as const) {
      putEvents(t.store, t.wg, [named(t, seq, body)]);
      expect(select(t)).toEqual({ status: 'unverifiable', code: 'params_invalid' });
    }
    // a name of 80 code points in astral characters is valid
    putEvents(t.store, t.wg, [named(t, 14, { name: '😀'.repeat(80) })]);
    expect(select(t)).toEqual({ status: 'given', text: '😀'.repeat(80) });
  });

  it('shows an unverifiable named event unless a newer verified one of the task is known', async () => {
    const t = await taskWorld();
    putEvents(t.store, t.wg, [named(t, 1, { name: 'Old' }), named(t, 2, null, { status: 'unverifiable', id: 'bad' })]);
    expect(select(t, 'bad')).toEqual({ status: 'unverifiable', code: 'signature_invalid' });
    // without the hub naming it, an unverifiable event cannot be attributed to the task
    expect(select(t)).toEqual({ status: 'given', text: 'Old' });
    putEvents(t.store, t.wg, [named(t, 3, { name: 'New' })]);
    expect(select(t, 'bad')).toEqual({ status: 'given', text: 'New' });
    // a raw event without a header names nothing
    putEvents(t.store, t.wg, [{ ...named(t, 4, null, { status: 'unverifiable' }), event: null }]);
    expect(select(t, 'n4')).toEqual({ status: 'given', text: 'New' });
  });

  it('ignores a verified event of another task that the row names', async () => {
    const t = await taskWorld();
    putEvents(t.store, t.wg, [named(t, 1, { name: 'Not mine' }, { task: OTHER })]);
    expect(select(t, 'n1')).toEqual({ status: 'automatic' });
  });
});

describe('agentLabel', () => {
  it('combines the name with the title', () => {
    const verified = { status: 'verified', text: 'Fix the login bug in the old portal' } as const;
    expect(agentLabel({ status: 'given', text: 'Doc' }, verified)).toEqual({ status: 'given', text: 'Doc' });
    expect(agentLabel({ status: 'automatic' }, verified)).toEqual({
      status: 'automatic',
      text: 'Fix the login bug in',
    });
    expect(agentLabel({ status: 'automatic' }, { status: 'missing' })).toEqual({ status: 'missing' });
    expect(agentLabel({ status: 'automatic' }, { status: 'unverifiable', code: 'x' })).toEqual({
      status: 'automatic',
      text: 'Agent',
    });
    expect(agentLabel({ status: 'missing' }, verified)).toEqual({ status: 'missing' });
    expect(agentLabel({ status: 'unverifiable', code: 'c' }, verified)).toEqual({ status: 'unverifiable', code: 'c' });
  });
});

describe('agentNames', () => {
  it('pulls the workgroup once, then each still missing task per named event; failures are retried', async () => {
    const t = await taskWorld();
    const route = `GET /v1/scopes/${t.wg}/events`;
    let failTask = true;
    t.client.on(route, (_body, target) => {
      const query = new URLSearchParams(target.split('?')[1]);
      if (query.get('task') === OTHER && failTask) throw new HubError('unavailable', 503);
      if (query.get('task') === TASK) putEvents(t.store, t.wg, [named(t, 1, { name: 'Pulled' })]);
      return { events: [], next_after: null };
    });
    const rows = [
      taskRow(t, { task_id: TASK, name_event_id: 'n1' }),
      taskRow(t, { task_id: OTHER, name_event_id: 'n2' }),
      taskRow(t, { task_id: 'no-name', name_event_id: null }),
      taskRow(t, { task_id: 'old-hub' }),
    ] as TaskRow[];
    await agentNames(t.context, rows);
    const targets = t.client.of(route).map((call) => new URLSearchParams(call.target.split('?')[1]));
    expect(targets.map((query) => [query.get('kinds'), query.get('task')])).toEqual([
      [AGENT_NAME_KIND, null],
      [AGENT_NAME_KIND, TASK],
      [AGENT_NAME_KIND, OTHER],
    ]);
    expect(select(t, 'n1')).toEqual({ status: 'given', text: 'Pulled' });
    // the failed pull is tried again, the done one is not
    failTask = false;
    await agentNames(t.context, rows);
    expect(t.client.of(route)).toHaveLength(4);
    await agentNames(t.context, rows);
    expect(t.client.of(route)).toHaveLength(4);
    // a new name event of the task is pulled again (an older name is shown meanwhile)
    const renamed = taskRow(t, { task_id: TASK, name_event_id: 'n7' }) as TaskRow;
    expect(select(t, 'n7')).toEqual({ status: 'given', text: 'Pulled' });
    await agentNames(t.context, [renamed]);
    expect(t.client.of(route)).toHaveLength(5);
  });
});

describe('agentNames when the workgroup pull fails', () => {
  it('still pulls the task of a named event', async () => {
    const t = await taskWorld();
    const route = `GET /v1/scopes/${t.wg}/events`;
    t.client.on(route, (_body, target) => {
      if (!target.includes('task=')) throw new HubError('unavailable', 503);
      return { events: [], next_after: null };
    });
    await agentNames(t.context, [taskRow(t, { task_id: TASK, name_event_id: 'n1' }) as TaskRow]);
    expect(t.client.of(route).map((call) => call.target.includes(`task=${TASK}`))).toEqual([false, true]);
  });
});

describe('renameAgent', () => {
  const answer =
    (t: TaskWorld, seq = 5) =>
    () => ({ results: [{ id: 'x', scope_id: t.wg, scope_seq: seq }] });

  it('seals task.agent_name under the workgroup key, appends it and reads it in', async () => {
    const t = await taskWorld();
    t.client.on('POST /v1/events', answer(t));
    const row = taskRow(t) as TaskRow;
    await renameAgent(t.context, row, '  Login doctor ');
    await renameAgent(t.context, row, null);
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const bodies = [];
    for (const call of t.client.of('POST /v1/events')) {
      const { event } = (call.body as { events: { event: unknown }[] }).events[0]!;
      const { checked, plaintext } = await openCheckedEvent(event, t.world.log!, key);
      expect(checked.header).toMatchObject({
        kind: AGENT_NAME_KIND,
        scope: WG,
        scope_id: t.wg,
        wg: t.wg,
        task: row.task_id,
        session: '',
        ref: '',
        author: t.me.id,
      });
      bodies.push(JSON.parse(fromUtf8(plaintext)));
    }
    expect(bodies).toEqual([{ name: 'Login doctor' }, { name: null }]);
    expect(t.ingested.map((item) => [item.org, item.scope, item.items[0]!.scope_seq])).toEqual([
      [t.org, t.wg, 5],
      [t.org, t.wg, 5],
    ]);
  });

  it('checks the input and the rights before sending, and the hub answer after', async () => {
    const t = await taskWorld();
    const row = taskRow(t) as TaskRow;
    expect(await rejects(renameAgent(t.context, row, ' '))).toBe('params_invalid');
    expect(await rejects(renameAgent(t.context, row, 'a\nb'))).toBe('params_invalid');
    expect(await rejects(renameAgent(t.context, { ...row, wg_id: 'unknown-wg' }, 'x'))).toBe('forbidden');
    expect(t.client.calls).toHaveLength(0);
    for (const bad of [null, {}, { results: [] }, { results: [{ scope_id: 'other', scope_seq: 1 }] }]) {
      t.client.on('POST /v1/events', () => bad);
      expect(await rejects(renameAgent(t.context, row, 'x'))).toBe('unavailable');
    }
    t.client.on('POST /v1/events', () => ({ results: [{ scope_id: t.wg, scope_seq: 'one' }] }));
    expect(await rejects(renameAgent(t.context, row, 'x'))).toBe('unavailable');
    t.client.on('POST /v1/events', () => {
      throw new HubError('author_not_writer', 403);
    });
    expect(await rejects(renameAgent(t.context, row, 'x'))).toBe('author_not_writer');
    expect(t.ingested).toHaveLength(0);
    blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: 3 });
    expect(await rejects(renameAgent(t.context, row, 'x'))).toBe('log_fork');
  });

  it('is offered to writers of the workgroup only', async () => {
    const t = await taskWorld();
    expect(canRename(t.store.getState(), t.org, t.wg)).toBe(true);
    expect(canRename(t.store.getState(), t.org, 'other-wg')).toBe(false);
    expect(canRename(t.store.getState(), 'other-org', t.wg)).toBe(false);
    t.store.update('me', (me) => ({ ...me, userId: null }));
    expect(canRename(t.store.getState(), t.org, t.wg)).toBe(false);
  });
});
