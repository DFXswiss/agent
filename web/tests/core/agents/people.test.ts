// KOR-52 (ADR-095 section 5, 06-web.md section 4.0a): loading the situation view by person – the person rows and a
// person's main agents, page by page (targets, cursors, malformed answers and other orgs' or people's tasks refused,
// failures recorded, the refresh after a reconnect, nothing after a sign-out), the order of the view and the
// attention record of an agent.
import { describe, expect, it } from 'vitest';
import {
  agentsTarget,
  isPersonRow,
  loadMainAgents,
  loadPeople,
  orderPeople,
  PAGE_SIZE,
  peopleTarget,
  selectAgentAttention,
  selectAgentIds,
} from '../../../src/core/agents/people';
import { HubError } from '../../../src/core/net/signedFetch';
import { flushAttention, queueAttention } from '../../../src/core/store/slices/attention';
import { agentList, peopleList, personOpen, setPersonOpen } from '../../../src/core/agents/state';
import type { PersonRow } from '../../../src/core/store/slices/mainAgents';
import { putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import { at, record } from '../attention/rows';
import { taskRow, type TaskWorld, taskWorld } from '../tasks/world';

const OWNER = '0190f000-0000-7000-8000-0000000000c1';

const person = (owner: string, overrides: Partial<PersonRow> = {}): PersonRow => ({
  owner_id: owner,
  agents: 3,
  needs_decision: 1,
  blocked: 0,
  working: 2,
  subagents_active: 4,
  ...overrides,
});

const people = (t: TaskWorld) => peopleList(t.store.getState().mainAgents, t.org);
const agents = (t: TaskWorld, owner = OWNER) => agentList(t.store.getState().mainAgents, t.org, owner);

const agentItem = (t: TaskWorld, task: string, minute: number, attention: unknown = null, row = {}) => ({
  task: taskRow(t, { task_id: task, owner_id: OWNER, last_activity_at: at(minute), ...row }),
  attention,
});

describe('targets', () => {
  it("page the people after an owner and a person's agents before a cursor", () => {
    expect(peopleTarget('o 1', null)).toBe(`/v1/orgs/o%201/people/agents?limit=${PAGE_SIZE}`);
    expect(peopleTarget('o', 'u')).toBe('/v1/orgs/o/people/agents?after=u&limit=50');
    expect(agentsTarget('o', 'u', null)).toBe('/v1/orgs/o/main-agents?owner=u&limit=50');
    expect(agentsTarget('o', 'u', 'c')).toBe('/v1/orgs/o/main-agents?owner=u&before=c&limit=50');
  });

  it('accept only well-formed person rows', () => {
    expect(isPersonRow(person(OWNER))).toBe(true);
    expect(isPersonRow(null)).toBe(false);
    expect(isPersonRow({ ...person(OWNER), owner_id: '' })).toBe(false);
    expect(isPersonRow({ ...person(OWNER), owner_id: 7 })).toBe(false);
    expect(isPersonRow({ ...person(OWNER), blocked: -1 })).toBe(false);
    expect(isPersonRow({ ...person(OWNER), working: 1.5 })).toBe(false);
  });
});

describe('people', () => {
  it('load the first and the next page; a person on two pages is listed once', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/people/agents`, (_body, target) =>
      target.includes('after=')
        ? { people: [person('b', { agents: 9 }), person('c')], next_after: null }
        : { people: [person('me'), person('b')], next_after: 'b' },
    );
    expect(await loadPeople(t.context, t.org)).toBe(2);
    expect(people(t)).toMatchObject({ owners: ['me', 'b'], nextAfter: 'b', status: 'ready', code: null });
    expect(await loadPeople(t.context, t.org, true)).toBe(2);
    expect(people(t).owners).toEqual(['me', 'b', 'c']);
    expect(people(t).rows.b!.agents).toBe(9);
    expect(t.client.calls.at(-1)!.target).toContain('after=b');
    // no further page: nothing is requested
    expect(await loadPeople(t.context, t.org, true)).toBe(0);
    expect(t.client.calls).toHaveLength(2);
    // the first page again starts the list over
    expect(await loadPeople(t.context, t.org)).toBe(2);
    expect(people(t).owners).toEqual(['me', 'b']);
  });

  it('leave a loading list alone, record failures, refuse malformed answers, refresh quietly', async () => {
    const t = await taskWorld();
    let answer: () => unknown = () => ({ people: [person('a')], next_after: null });
    t.client.on(`GET /v1/orgs/${t.org}/people/agents`, () => answer());
    const first = loadPeople(t.context, t.org);
    expect(await loadPeople(t.context, t.org)).toBe(0);
    expect(await first).toBe(1);
    for (const bad of [
      null,
      { people: 'x', next_after: null },
      { people: [{ owner_id: 'a' }], next_after: null },
      { people: [], next_after: 7 },
    ]) {
      answer = () => bad;
      expect(await loadPeople(t.context, t.org)).toBe(0);
      expect(people(t)).toMatchObject({ status: 'error', code: 'unavailable', owners: ['a'] });
    }
    answer = () => {
      throw new HubError('forbidden', 403);
    };
    expect(await loadPeople(t.context, t.org)).toBe(0);
    expect(people(t).code).toBe('forbidden');
    // a refresh that fails leaves the list as it was
    expect(await loadPeople(t.context, t.org, false, true)).toBe(0);
    expect(people(t).code).toBe('forbidden');
    answer = () => ({ people: [person('z')], next_after: null });
    expect(await loadPeople(t.context, t.org, false, true)).toBe(1);
    expect(people(t)).toMatchObject({ owners: ['z'], status: 'ready' });
  });

  it('write nothing after a sign-out', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/people/agents`, () => {
      t.store.reset();
      return { people: [person('a')], next_after: null };
    });
    expect(await loadPeople(t.context, t.org)).toBe(0);
    expect(people(t).owners).toEqual([]);
    t.client.on(`GET /v1/orgs/${t.org}/people/agents`, () => {
      t.store.reset();
      throw new HubError('unavailable', 503);
    });
    expect(await loadPeople(t.context, t.org)).toBe(0);
    expect(people(t).status).toBe('idle');
  });
});

describe('main agents', () => {
  it('load the pages of one person with their tasks and attention; other orgs and people are refused', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, (_body, target) =>
      target.includes('before=')
        ? { items: [agentItem(t, 'c', 1)], next_before: null }
        : {
            items: [
              agentItem(t, 'a', 5, record('a', { category: 'working', reason: 'working' })),
              agentItem(t, 'b', 4),
              agentItem(t, 'x', 3, null, { org_id: 'other' }),
              agentItem(t, 'y', 3, null, { owner_id: 'someone-else' }),
            ],
            next_before: 'cur',
          },
    );
    expect(await loadMainAgents(t.context, t.org, OWNER)).toBe(2);
    expect(agents(t)).toMatchObject({ ids: ['a', 'b'], nextBefore: 'cur', status: 'ready' });
    expect(t.client.calls[0]!.target).toContain(`owner=${OWNER}`);
    expect(t.store.getState().tasks.tasks.a).toBeDefined();
    expect(t.store.getState().tasks.tasks.x).toBeUndefined();
    expect(t.store.getState().tasks.tasks.y).toBeUndefined();
    expect(t.store.getState().mainAgents.attention.a!.reason).toBe('working');
    expect(await loadMainAgents(t.context, t.org, OWNER, true)).toBe(1);
    expect(agents(t).ids).toEqual(['a', 'b', 'c']);
    expect(t.client.calls.at(-1)!.target).toContain('before=cur');
    expect(await loadMainAgents(t.context, t.org, OWNER, true)).toBe(0);
    expect(t.client.calls).toHaveLength(2);
    // another person's list is its own
    expect(agents(t, 'someone-else').status).toBe('idle');
  });

  it('leave a loading list alone, record failures, refuse malformed answers, refresh quietly', async () => {
    const t = await taskWorld();
    let answer: () => unknown = () => ({ items: [agentItem(t, 'a', 1)], next_before: null });
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, () => answer());
    const first = loadMainAgents(t.context, t.org, OWNER);
    expect(await loadMainAgents(t.context, t.org, OWNER)).toBe(0);
    expect(await first).toBe(1);
    for (const bad of [
      null,
      { items: 'x', next_before: null },
      { items: [{ task: {}, attention: null }], next_before: null },
      { items: [agentItem(t, 'a', 1, record('other'))], next_before: null },
      { items: [agentItem(t, 'a', 1, { task_id: 'a' })], next_before: null },
      { items: [], next_before: 7 },
    ]) {
      answer = () => bad;
      expect(await loadMainAgents(t.context, t.org, OWNER)).toBe(0);
      expect(agents(t)).toMatchObject({ status: 'error', code: 'unavailable', ids: ['a'] });
    }
    expect(await loadMainAgents(t.context, t.org, OWNER, false, true)).toBe(0);
    expect(agents(t).status).toBe('error');
    answer = () => ({ items: [agentItem(t, 'b', 2)], next_before: null });
    expect(await loadMainAgents(t.context, t.org, OWNER, false, true)).toBe(1);
    expect(agents(t)).toMatchObject({ ids: ['b'], status: 'ready' });
  });

  it('write nothing after a sign-out', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, () => {
      t.store.reset();
      return { items: [agentItem(t, 'a', 1)], next_before: null };
    });
    expect(await loadMainAgents(t.context, t.org, OWNER)).toBe(0);
    expect(agents(t).ids).toEqual([]);
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, () => {
      t.store.reset();
      throw new HubError('unavailable', 503);
    });
    expect(await loadMainAgents(t.context, t.org, OWNER)).toBe(0);
    expect(agents(t).status).toBe('idle');
  });
});

describe('order and selectors', () => {
  it('puts the user first, the others by name, then by ID', () => {
    const names: Record<string, string> = { a: 'Zoe', b: 'Anna', c: 'Anna', me: 'Zed' };
    expect(orderPeople(['a', 'c', 'me', 'b'], 'me', (id) => names[id]!)).toEqual(['me', 'b', 'c', 'a']);
    expect(orderPeople(['a', 'b'], null, (id) => names[id]!)).toEqual(['b', 'a']);
  });

  it('lists the open agents newest activity first and keeps the array while nothing changed', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, () => ({
      items: [agentItem(t, 'a', 5), agentItem(t, 'b', 4), agentItem(t, 'c', 3)],
      next_before: null,
    }));
    const select = selectAgentIds(t.org, OWNER);
    expect(select(t.store.getState())).toEqual([]);
    await loadMainAgents(t.context, t.org, OWNER);
    const first = select(t.store.getState());
    expect(first).toEqual(['a', 'b', 'c']);
    expect(select(t.store.getState())).toBe(first);
    // a change elsewhere keeps the array
    putTasks(t.store, [taskRow(t, { task_id: 'z', last_activity_at: at(9) }) as TaskRow]);
    expect(select(t.store.getState())).toBe(first);
    // newer activity moves an agent up, a closed one leaves the list
    putTasks(t.store, [taskRow(t, { task_id: 'c', owner_id: OWNER, last_activity_at: at(8) }) as TaskRow]);
    expect(select(t.store.getState())).toEqual(['c', 'a', 'b']);
    putTasks(t.store, [
      taskRow(t, { task_id: 'a', owner_id: OWNER, last_activity_at: at(9), state: 'closed' }) as TaskRow,
    ]);
    expect(select(t.store.getState())).toEqual(['c', 'b']);
    // a list whose tasks are all unknown is empty
    t.store.update('tasks', (state) => ({ ...state, tasks: {} }));
    expect(select(t.store.getState())).toEqual([]);
  });

  it('takes the newer of the live and the paged attention record', async () => {
    const t = await taskWorld();
    const select = selectAgentAttention('a');
    expect(select(t.store.getState())).toBeNull();
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, () => ({
      items: [agentItem(t, 'a', 1, record('a', { reason: 'approval', updated_at: at(5) }))],
      next_before: null,
    }));
    await loadMainAgents(t.context, t.org, OWNER);
    expect(select(t.store.getState())!.reason).toBe('approval');
    t.store.update('session', (session) => ({ ...session, activeOrg: t.org }));
    queueAttention(t.store, record('a', { reason: 'question', updated_at: at(3) }));
    flushAttention(t.store);
    expect(select(t.store.getState())!.reason).toBe('approval');
    queueAttention(t.store, record('a', { reason: 'question', updated_at: at(7) }));
    flushAttention(t.store);
    expect(select(t.store.getState())!.reason).toBe('question');
    queueAttention(t.store, record('b', { reason: 'question' }));
    flushAttention(t.store);
    expect(selectAgentAttention('b')(t.store.getState())!.reason).toBe('question');
  });
});

describe('open rows', () => {
  it('are kept per org and owner until closed', async () => {
    const t = await taskWorld();
    const open = (org: string, owner: string) => personOpen(t.store.getState().mainAgents, org, owner);
    expect(open(t.org, OWNER)).toBe(false);
    setPersonOpen(t.store, t.org, OWNER, true);
    setPersonOpen(t.store, t.org, OWNER, true);
    expect(open(t.org, OWNER)).toBe(true);
    expect(open('other-org', OWNER)).toBe(false);
    setPersonOpen(t.store, t.org, OWNER, false);
    expect(open(t.org, OWNER)).toBe(false);
    expect(t.store.getState().mainAgents.open).toEqual({});
  });
});
