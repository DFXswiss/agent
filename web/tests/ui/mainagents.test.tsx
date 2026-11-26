// U-WEB-ui-mainagents (KOR-52, ADR-095, 06-web.md section 4.0a): the situation view by person in jsdom against the
// core test world – the switch kept in the URL, the person rows (the user first as "You", the others by name, the
// counts), a person's agents (name and handle, reason, GitHub identity, helpers, "Rename" for writers only, the tree
// with handles, "triggered by" and a child's own identity), unreadable workgroups, errors, reconnects and the windows
// of long lists; the agent name in the task header (rename, automatic name, refused input, failures) and its states
// (loading, unverifiable); the sessions tab with handles. The Playwright specs run the same views against the hub.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { fromUtf8 } from '../../src/core/encoding/bytes';
import { openEvent } from '../../src/core/crypto/event';
import { createI18n } from '../../src/core/i18n';
import { epochKey, WG } from '../../src/core/log/state';
import { applyFrame, frameHandlers } from '../../src/core/net/frames';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import type { PersonRow } from '../../src/core/store/slices/mainAgents';
import { setLog } from '../../src/core/store/slices/orgs';
import { putSession, putTasks, type SessionRow, type TaskRow } from '../../src/core/store/slices/tasks';
import { AgentName } from '../../src/ui/agents/AgentName';
import { I18nProvider } from '../../src/ui/i18n';
import { HomeScreen } from '../../src/ui/screens/home/HomeScreen';
import { TaskAgent } from '../../src/ui/screens/task/TaskAgent';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { SessionsTab } from '../../src/ui/task-tabs/sessions/SessionsTab';
import { idOf } from '../core/log/world';
import { at, record } from '../core/attention/rows';
import { sessionRow, taskRow, type TaskWorld, taskWorld, type WorldOptions } from '../core/tasks/world';

const ANNA = '0190f000-0000-7000-8000-0000000000c1';
const ZOE = '0190f000-0000-7000-8000-0000000000c2';
const NOBODY = '0190f000-0000-7000-8000-0000000000c3';
const T1 = '0190f000-0000-7000-8000-000000000101';
const T2 = '0190f000-0000-7000-8000-000000000102';
const IDENTITY = '0190f000-0000-7000-8000-0000000000e1';
const SUMMARY = { needs_decision: 0, needs_decision_mine: 0, blocked: 0, working: 2, done_since: 0 };

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function wrap(t: TaskWorld, children: ReactNode) {
  const app = { store: t.store, tasks: t.context } as never;
  return (
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>{children}</I18nProvider>
    </AppEnvProvider>
  );
}

function mountHome(t: TaskWorld, query = '?by=person') {
  const router = createMemoryRouter(
    [
      { path: '/o/:org', element: <HomeScreen /> },
      { path: '*', element: <p data-testid="elsewhere">elsewhere</p> },
    ],
    { initialEntries: [`/o/${t.org}${query}`] },
  );
  render(wrap(t, <RouterProvider router={router} />));
  return router;
}

const person = (owner: string, overrides: Partial<PersonRow> = {}): PersonRow => ({
  owner_id: owner,
  agents: 2,
  needs_decision: 1,
  blocked: 0,
  working: 1,
  subagents_active: 3,
  ...overrides,
});

const agentItem = (t: TaskWorld, task: string, minute: number, attention: unknown, row = {}) => ({
  task: taskRow(t, { task_id: task, main_session_id: null, last_activity_at: at(minute), ...row }),
  attention,
});

/** A verified event as the reader puts it into the store. */
const verified = (
  t: TaskWorld,
  scope: string,
  seq: number,
  header: Record<string, unknown>,
  body: unknown,
): EventEntry => ({
  org: t.org,
  scopeId: scope,
  scopeSeq: seq,
  receivedAt: null,
  event: { header },
  status: 'verified',
  code: null,
  header: { id: `e${scope}${seq}`, ts: '2026-11-21T12:00:00.000Z', ...header },
  body,
});

/** A world whose hub answers the people, a person's agents, the attention groups and the counters. */
async function world(
  people: (t: TaskWorld) => unknown[] = () => [],
  agents: (t: TaskWorld, owner: string) => unknown[] = () => [],
  options: WorldOptions = {},
) {
  const t = await taskWorld(options);
  t.client.on(`GET /v1/orgs/${t.org}/people/agents`, () => ({ people: people(t), next_after: null }));
  t.client.on(`GET /v1/orgs/${t.org}/main-agents`, (_body, target) => {
    const owner = new URLSearchParams(target.split('?')[1]).get('owner')!;
    return { items: agents(t, owner), next_before: null };
  });
  t.client.on(`GET /v1/orgs/${t.org}/attention`, () => ({ items: [], next_before: null }));
  t.client.on(`GET /v1/orgs/${t.org}/attention/summary`, () => SUMMARY);
  for (const scope of [t.org, t.wg])
    t.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null, head: 0 }));
  putEvents(t.store, t.org, [
    verified(t, t.org, 1, { kind: 'member.profile', ref: ANNA, author: ANNA, wg: '' }, { display_name: 'Anna' }),
    verified(t, t.org, 2, { kind: 'member.profile', ref: ZOE, author: ZOE, wg: '' }, { display_name: 'Zoe' }),
  ]);
  return t;
}

const personRow = (owner: string) =>
  screen.getAllByTestId('agents.person').find((node) => node.dataset.owner === owner)!;
const agentRow = (task: string) => screen.getAllByTestId('agents.item').find((node) => node.dataset.task === task)!;

describe('switch', () => {
  it('groups the situation by situation or by person and keeps the choice in the URL', async () => {
    const t = await world((w) => [person(w.me.id)]);
    const router = mountHome(t, '');
    const toggle = await screen.findByTestId('agents.switch');
    expect(toggle).toHaveAttribute('data-by', 'situation');
    expect(screen.getByTestId('agents.switch.situation')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('attention.group.working')).toBeInTheDocument();
    expect(screen.queryByTestId('agents.people')).toBeNull();
    fireEvent.click(screen.getByTestId('agents.switch.person'));
    expect(router.state.location.search).toBe('?by=person');
    expect(screen.getByTestId('agents.switch')).toHaveAttribute('data-by', 'person');
    // the counters stay, the groups give way to the people
    expect(screen.getByTestId('attention.counters')).toBeInTheDocument();
    expect(screen.queryByTestId('attention.group.working')).toBeNull();
    await waitFor(() => expect(screen.getAllByTestId('agents.person')).toHaveLength(1));
    fireEvent.click(screen.getByTestId('agents.switch.situation'));
    expect(router.state.location.search).toBe('');
    expect(screen.getByTestId('attention.group.working')).toBeInTheDocument();
  });
});

describe('people', () => {
  it('lists the user first as "You", the others by name, with their counts', async () => {
    const t = await world((w) => [
      person(ZOE, { agents: 1234 }),
      person(NOBODY),
      person(w.me.id, { agents: 3 }),
      person(ANNA),
    ]);
    mountHome(t);
    await waitFor(() => expect(screen.getAllByTestId('agents.person')).toHaveLength(4));
    expect(screen.getAllByTestId('agents.person').map((node) => node.dataset.owner)).toEqual([
      t.me.id,
      ANNA,
      NOBODY,
      ZOE,
    ]);
    expect(within(personRow(t.me.id)).getByTestId('agents.person.name')).toHaveTextContent('You');
    expect(within(personRow(NOBODY)).getByTestId('agents.person.name')).toHaveTextContent('Name not known yet');
    expect(within(personRow(ZOE)).getByTestId('agents.person.count.agents')).toHaveTextContent('1,234');
    const anna = personRow(ANNA);
    expect(within(anna).getByTestId('agents.person.count.agents')).toHaveTextContent('2');
    expect(within(anna).getByTestId('agents.person.count.needs_decision')).toHaveTextContent('1');
    expect(within(anna).getByTestId('agents.person.count.blocked')).toHaveTextContent('0');
    expect(within(anna).getByTestId('agents.person.count.working')).toHaveTextContent('1');
    expect(within(anna).getByTestId('agents.person.count.subagents')).toHaveTextContent('3');
    expect(anna).toHaveTextContent('2 agents1 waiting for a decision0 stuck1 running3 helpers active');
    expect(screen.queryByTestId('agents.people.more')).toBeNull();
  });

  it('says when nobody has an open agent, reports a failed load and loads again after a reconnect', async () => {
    const t = await world();
    let fail = true;
    t.client.on(`GET /v1/orgs/${t.org}/people/agents`, () => {
      if (fail) throw new HubError('unavailable', 503);
      return { people: [], next_after: null };
    });
    mountHome(t);
    expect(screen.getByTestId('agents.people.loading')).toHaveTextContent('Loading the people…');
    expect(await screen.findByTestId('agents.people.error')).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByTestId('agents.people.reload'));
    expect(await screen.findByTestId('agents.people.empty')).toHaveTextContent(
      'Nobody has an open agent in the workgroups you can read.',
    );
    const calls = t.client.of(`GET /v1/orgs/${t.org}/people/agents`).length;
    act(() => {
      applyFrame(t.store, 'hello', { heads: {}, hub_time: '2026-11-22T12:00:00Z' }, 0, frameHandlers());
    });
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/people/agents`).length).toBe(calls + 1));
  });
});

describe("a person's agents", () => {
  it('shows name and handle, reason, identity, helpers, rename and the tree with handles', async () => {
    const t = await world(
      (w) => [person(w.me.id)],
      (w) => [
        agentItem(
          w,
          T1,
          5,
          record(T1, { category: 'working', reason: 'subagents', subagents_total: 2, subagents_active: 1 }),
          { number: 1, title_event_id: null },
        ),
        agentItem(w, T2, 4, null, {
          number: 2,
          github_identity: IDENTITY,
          name_event_id: `e${w.wg}2`,
          agent_profile: { name: 'review', operations: ['review.verdict'], reads: 'all' },
        }),
      ],
    );
    putEvents(t.store, t.wg, [
      verified(
        t,
        t.wg,
        1,
        { kind: 'task.meta', task: T1, author: t.me.id },
        { title: 'Fix the login bug in the portal' },
      ),
      verified(t, t.wg, 2, { kind: 'task.agent_name', task: T2, author: t.me.id }, { name: 'Login doctor' }),
    ]);
    t.client.on('POST /v1/events', () => ({ results: [{ id: 'x', scope_id: t.wg, scope_seq: 9 }] }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      if (target.includes(`task=${T2}`)) throw new HubError('unavailable', 503);
      return { events: [], next_after: null };
    });
    t.client.on(`GET /v1/tasks/${T1}`, () => ({
      task: taskRow(t, { task_id: T1, main_session_id: 'm', number: 1 }),
      sessions: [
        { session: sessionRow(t, { session_id: 'm', task_id: T1, state: 'working', triggered_by: t.me.id }) },
        {
          session: sessionRow(t, {
            session_id: 'c',
            task_id: T1,
            parent_session_id: 'm',
            depth: 1,
            agent_path: '1',
            state: 'working',
            created_at: at(1),
            github_identity: IDENTITY,
            origin_operation_id: '0190f000-0000-7000-8000-0000000000b1',
          }),
        },
      ],
    }));
    mountHome(t);
    const toggle = await screen.findByTestId('agents.person.toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent('Show agents');
    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent('Hide agents');
    await waitFor(() => expect(screen.getAllByTestId('agents.item')).toHaveLength(2));
    expect(t.client.of(`GET /v1/orgs/${t.org}/main-agents`)[0]!.target).toContain(`owner=${t.me.id}`);
    expect(screen.getAllByTestId('agents.item').map((node) => node.dataset.task)).toEqual([T1, T2]);
    const first = agentRow(T1);
    expect(within(first).getByTestId('agents.item.name')).toHaveTextContent('Fix the login bug in');
    expect(within(first).getByTestId('agents.item.name')).toHaveAttribute('data-status', 'automatic');
    expect(within(first).getByTestId('agents.item.handle')).toHaveTextContent('#1');
    expect(within(first).getByTestId('agents.item.reason')).toHaveTextContent('Helpers are working');
    expect(within(first).getByTestId('agents.item.identity')).toHaveTextContent('GitHub: Automatic');
    expect(within(first).getByTestId('agents.item.subagents')).toHaveTextContent('1 of 2 helpers active');
    expect(within(first).getByTestId('agents.item.open')).toHaveAttribute('href', `/o/${t.org}/t/${T1}`);
    const second = agentRow(T2);
    expect(within(second).getByTestId('agents.item.name')).toHaveTextContent('Login doctor');
    expect(within(second).getByTestId('agents.item.handle')).toHaveTextContent('#2');
    expect(within(second).getByTestId('agents.item.reason')).toHaveTextContent('No situation known yet');
    expect(within(second).getByTestId('agents.item.identity')).toHaveTextContent('GitHub: another account');
    expect(within(second).queryByTestId('agents.item.subagents')).toBeNull();
    // the main agent's role is its task's rights profile (KOR-51); a task without one shows none
    expect(within(second).getByTestId('session.role')).toHaveAttribute('data-role', 'review');
    expect(within(first).queryByTestId('session.role')).toBeNull();

    // the tree: handles, "triggered by", the identity of a child that acts as another account
    fireEvent.click(within(first).getByTestId('agents.item.toggle'));
    await waitFor(() => expect(within(first).getAllByTestId('attention.tree.node')).toHaveLength(2));
    const nodes = within(first).getAllByTestId('attention.tree.node');
    expect(nodes.map((node) => node.dataset.handle)).toEqual(['#1', '#1.1']);
    expect(nodes[0]).toHaveTextContent('Main agent #1');
    expect(within(nodes[0]!).getByTestId('attention.tree.triggered-by')).toHaveTextContent('Triggered by you');
    expect(within(nodes[0]!).queryByTestId('attention.tree.identity')).toBeNull();
    expect(nodes[1]).toHaveTextContent('Subagent #1.1');
    expect(within(nodes[1]!).getByTestId('attention.tree.identity')).toHaveTextContent('acts as another account');
    fireEvent.click(within(first).getByTestId('agents.item.toggle'));
    expect(within(first).queryByTestId('attention.tree')).toBeNull();
    // an agent without a known situation opens its tree too (the purposes cannot be read: none are shown)
    fireEvent.click(within(second).getByTestId('agents.item.toggle'));
    expect(within(second).getByTestId('attention.tree')).toBeInTheDocument();
    await waitFor(() =>
      expect(
        t.client.of(`GET /v1/scopes/${t.wg}/events`).filter((call) => call.target.includes(`task=${T2}`)).length,
      ).toBeGreaterThan(0),
    );

    // rename from the row
    fireEvent.click(within(first).getByTestId('agents.item.rename'));
    expect(within(first).getByTestId('agents.item.rename.input')).toHaveValue('Fix the login bug in');
    fireEvent.change(within(first).getByTestId('agents.item.rename.input'), { target: { value: 'Login fixer' } });
    fireEvent.click(within(first).getByTestId('agents.item.rename.save'));
    await waitFor(() => expect(within(first).queryByTestId('agents.item.rename.input')).toBeNull());
    expect(t.client.of('POST /v1/events')).toHaveLength(1);

    fireEvent.click(toggle);
    expect(screen.queryByTestId('agents.item')).toBeNull();
  });

  it('offers no rename to readers, hides unreadable workgroups and says when a person has no open agent', async () => {
    const t = await world(
      (w) => [person(w.me.id), person(ANNA)],
      (w, owner) =>
        owner === ANNA
          ? []
          : [
              agentItem(w, T1, 5, null, { number: 1, wg_id: readOnly }),
              agentItem(w, T2, 4, null, { number: 2, wg_id: secret }),
            ],
      { member: true, private: false },
    );
    const readOnly = await idOf('read only');
    const secret = await idOf('secret');
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: readOnly, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: readOnly, access: 'read' },
      { type: 'wg_create', wg: secret, epoch: 1 },
    ]);
    setLog(t.store, t.org, t.world.log!);
    mountHome(t);
    await waitFor(() => expect(screen.getAllByTestId('agents.person')).toHaveLength(2));
    fireEvent.click(within(personRow(t.me.id)).getByTestId('agents.person.toggle'));
    await waitFor(() => expect(screen.getAllByTestId('agents.item')).toHaveLength(1));
    expect(within(agentRow(T1)).queryByTestId('agents.item.rename')).toBeNull();
    fireEvent.click(within(personRow(ANNA)).getByTestId('agents.person.toggle'));
    expect(await within(personRow(ANNA)).findByTestId('agents.empty')).toHaveTextContent('No open agent.');
  });

  it('keeps an opened row open across a refresh and a remount of the view', async () => {
    const t = await world(
      (w) => [person(w.me.id), person(ANNA)],
      (w, owner) => (owner === ANNA ? [] : [agentItem(w, T1, 5, null, { number: 1 })]),
    );
    mountHome(t);
    await waitFor(() => expect(screen.getAllByTestId('agents.person')).toHaveLength(2));
    fireEvent.click(within(personRow(t.me.id)).getByTestId('agents.person.toggle'));
    await waitFor(() => expect(screen.getAllByTestId('agents.item')).toHaveLength(1));
    // a refresh of the people after a reconnect
    act(() => {
      applyFrame(t.store, 'hello', { heads: {}, hub_time: '2026-11-22T12:00:00Z' }, 0, frameHandlers());
    });
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/people/agents`).length).toBe(2));
    expect(within(personRow(t.me.id)).getByTestId('agents.person.toggle')).toHaveAttribute('aria-expanded', 'true');
    // the view is mounted anew (here: by situation and back)
    fireEvent.click(screen.getByTestId('agents.switch.situation'));
    fireEvent.click(screen.getByTestId('agents.switch.person'));
    await waitFor(() => expect(screen.getAllByTestId('agents.person')).toHaveLength(2));
    expect(within(personRow(t.me.id)).getByTestId('agents.person.toggle')).toHaveAttribute('aria-expanded', 'true');
    expect(within(personRow(ANNA)).getByTestId('agents.person.toggle')).toHaveAttribute('aria-expanded', 'false');
    await waitFor(() => expect(screen.getAllByTestId('agents.item')).toHaveLength(1));
  });

  it('reports a failed load, retries it and loads again after a reconnect', async () => {
    const t = await world((w) => [person(w.me.id)]);
    let fail = true;
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, () => {
      if (fail) throw new HubError('unavailable', 503);
      return { items: [agentItem(t, T1, 1, null, { owner_id: t.me.id })], next_before: null };
    });
    mountHome(t);
    fireEvent.click(await screen.findByTestId('agents.person.toggle'));
    expect(screen.getByTestId('agents.loading')).toHaveTextContent('Loading the agents…');
    expect(await screen.findByTestId('agents.error')).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByTestId('agents.reload'));
    await waitFor(() => expect(screen.getAllByTestId('agents.item')).toHaveLength(1));
    const calls = t.client.of(`GET /v1/orgs/${t.org}/main-agents`).length;
    act(() => {
      applyFrame(t.store, 'hello', { heads: {}, hub_time: '2026-11-22T12:00:00Z' }, 0, frameHandlers());
    });
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/main-agents`).length).toBe(calls + 1));
  });
});

describe('windows', () => {
  function observe() {
    const observed: { callback: IntersectionObserverCallback | null } = { callback: null };
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback: IntersectionObserverCallback) {
          observed.callback = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    return observed;
  }

  it('keep at most 50 people in the DOM, page from the hub and move by 25', async () => {
    const t = await world();
    const owners = Array.from(
      { length: 120 },
      (_, index) => `0190f000-0000-7000-8000-${String(index).padStart(12, '0')}`,
    );
    t.client.on(`GET /v1/orgs/${t.org}/people/agents`, (_body, target) => {
      const after = new URLSearchParams(target.split('?')[1]).get('after');
      const start = after === null ? 0 : owners.indexOf(after) + 1;
      const page = owners.slice(start, start + 50);
      return {
        people: page.map((owner) => person(owner)),
        next_after: start + 50 < owners.length ? page.at(-1) : null,
      };
    });
    const observed = observe();
    mountHome(t);
    const view = () => screen.getByTestId('agents.people');
    await waitFor(() => expect(screen.getAllByTestId('agents.person')).toHaveLength(50));
    expect(view()).toHaveAttribute('data-total', '50');
    expect(screen.getByTestId('agents.people.more')).toHaveTextContent('Load more people');
    await act(async () =>
      observed.callback!([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
    );
    await waitFor(() => expect(view()).toHaveAttribute('data-from', '25'));
    expect(view()).toHaveAttribute('data-total', '100');
    expect(screen.getAllByTestId('agents.person')).toHaveLength(50);
    expect(screen.getByTestId('agents.people.more')).toHaveTextContent('Show later people');
    await act(async () =>
      observed.callback!([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver),
    );
    fireEvent.click(screen.getByTestId('agents.people.more'));
    await waitFor(() => expect(view()).toHaveAttribute('data-from', '50'));
    fireEvent.click(screen.getByTestId('agents.people.more'));
    await waitFor(() => expect(view()).toHaveAttribute('data-total', '120'));
    await waitFor(() => expect(view()).toHaveAttribute('data-from', '70'));
    expect(screen.queryByTestId('agents.people.more')).toBeNull();
    fireEvent.click(screen.getByTestId('agents.people.earlier'));
    expect(view()).toHaveAttribute('data-from', '45');
  });

  it("keep at most 50 of a person's agents in the DOM and page from the hub", async () => {
    const t = await world((w) => [person(w.me.id)]);
    const tasks = Array.from(
      { length: 80 },
      (_, index) => `0190f000-0000-7000-8000-${String(index).padStart(12, '0')}`,
    );
    t.client.on(`GET /v1/orgs/${t.org}/main-agents`, (_body, target) => {
      const start = Number(new URLSearchParams(target.split('?')[1]).get('before') ?? 0);
      const items = tasks
        .slice(start, start + 50)
        .map((task, index) =>
          agentItem(t, task, 0, null, { last_activity_at: at(59 - Math.floor((start + index) / 2)) }),
        );
      return { items, next_before: start + 50 < tasks.length ? String(start + 50) : null };
    });
    const observed = observe();
    mountHome(t);
    fireEvent.click(await screen.findByTestId('agents.person.toggle'));
    const list = () => screen.getByTestId('agents.list');
    await waitFor(() => expect(screen.getAllByTestId('agents.item')).toHaveLength(50));
    expect(screen.getByTestId('agents.more')).toHaveTextContent('Load more agents');
    await act(async () =>
      observed.callback!([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
    );
    await waitFor(() => expect(list()).toHaveAttribute('data-total', '80'));
    await waitFor(() => expect(list()).toHaveAttribute('data-from', '25'));
    expect(screen.getAllByTestId('agents.item')).toHaveLength(50);
    fireEvent.click(screen.getByTestId('agents.more'));
    await waitFor(() => expect(list()).toHaveAttribute('data-from', '30'));
    expect(screen.queryByTestId('agents.more')).toBeNull();
    fireEvent.click(screen.getByTestId('agents.earlier'));
    expect(list()).toHaveAttribute('data-from', '5');
    expect(screen.getByTestId('agents.more')).toHaveTextContent('Show later agents');
    // the first agent of the moved window closes: the window stays at its place
    const first = screen.getAllByTestId('agents.item')[0]!.dataset.task!;
    act(() =>
      putTasks(t.store, [
        taskRow(t, { task_id: first, state: 'closed', last_activity_at: '2026-11-21T13:00:00.000Z' }) as TaskRow,
      ]),
    );
    expect(list()).toHaveAttribute('data-total', '79');
    expect(list()).toHaveAttribute('data-from', '5');
    expect(screen.getAllByTestId('agents.item')[0]!.dataset.task).not.toBe(first);
    // the shown agents' titles are pulled once the window rests
    await waitFor(() =>
      expect(t.client.of(`GET /v1/scopes/${t.wg}/events`).some((call) => call.target.includes('kinds=task.meta'))).toBe(
        true,
      ),
    );
  });
});

describe('task header', () => {
  function header(t: TaskWorld, row: TaskRow, writer = true) {
    render(wrap(t, <TaskAgent row={row} writer={writer} />));
  }

  /** The names sent with `POST /v1/events` (opened with the workgroup key). */
  async function sentNames(t: TaskWorld): Promise<unknown[]> {
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const sig = b64uDecode(t.me.identity.public().sig);
    return Promise.all(
      t.client.of('POST /v1/events').map(async (call) => {
        const { event } = (call.body as { events: { event: never }[] }).events[0]!;
        return JSON.parse(fromUtf8(await openEvent(key, event, sig))) as unknown;
      }),
    );
  }

  it('renames the agent, goes back to the automatic name and refuses an invalid name', async () => {
    const t = await taskWorld();
    let fail = false;
    t.client.on('POST /v1/events', () => {
      if (fail) throw new HubError('author_not_writer', 403);
      return { results: [{ id: 'x', scope_id: t.wg, scope_seq: 9 }] };
    });
    const row = taskRow(t, { number: 42 }) as TaskRow;
    putEvents(t.store, t.wg, [
      verified(t, t.wg, 1, { kind: 'task.meta', task: row.task_id, author: t.me.id }, { title: 'Fix it' }),
    ]);
    header(t, row);
    expect(screen.getByTestId('task.agent.name')).toHaveTextContent('Fix it');
    expect(screen.getByTestId('task.agent.handle')).toHaveTextContent('#42');
    fireEvent.click(screen.getByTestId('task.agent.rename'));
    const input = screen.getByTestId('task.agent.rename.input');
    expect(input).toHaveValue('Fix it');
    fireEvent.change(input, { target: { value: '   ' } });
    expect(screen.getByTestId('task.agent.rename.save')).toBeDisabled();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('task.agent.rename.form')).toHaveTextContent(
      'The name must have 1 to 80 characters on one line.',
    );
    // submitting an invalid name sends nothing
    fireEvent.submit(screen.getByTestId('task.agent.rename.form'));
    expect(t.client.of('POST /v1/events')).toHaveLength(0);
    fireEvent.change(input, { target: { value: ' Login doctor ' } });
    fireEvent.submit(screen.getByTestId('task.agent.rename.form'));
    await waitFor(() => expect(screen.queryByTestId('task.agent.rename.form')).toBeNull());
    fireEvent.click(screen.getByTestId('task.agent.rename'));
    fireEvent.click(screen.getByTestId('task.agent.rename.automatic'));
    await waitFor(() => expect(screen.queryByTestId('task.agent.rename.form')).toBeNull());
    expect(await sentNames(t)).toEqual([{ name: 'Login doctor' }, { name: null }]);
    // a refused name stays in the field with the reason; cancel closes it
    fail = true;
    fireEvent.click(screen.getByTestId('task.agent.rename'));
    fireEvent.click(screen.getByTestId('task.agent.rename.save'));
    expect(await screen.findByTestId('task.agent.rename.error')).toHaveAttribute('data-code', 'author_not_writer');
    fireEvent.click(screen.getByTestId('task.agent.rename.cancel'));
    expect(screen.queryByTestId('task.agent.rename.form')).toBeNull();
    fireEvent.click(screen.getByTestId('task.agent.rename'));
    expect(screen.queryByTestId('task.agent.rename.error')).toBeNull();
  });

  it('offers no rename to readers and shows the name states', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    const row = taskRow(t, { name_event_id: 'named' }) as TaskRow;
    header(t, row, false);
    expect(screen.queryByTestId('task.agent.rename')).toBeNull();
    // the named event is not read yet: loading, and no handle from a hub without numbers
    expect(screen.getByTestId('task.agent.name')).toHaveTextContent('Loading the name…');
    expect(screen.getByTestId('task.agent.name')).toHaveAttribute('data-status', 'missing');
    expect(screen.queryByTestId('task.agent.handle')).toBeNull();
    await waitFor(() => expect(t.client.of(`GET /v1/scopes/${t.wg}/events`).length).toBeGreaterThan(0));
    act(() =>
      putEvents(t.store, t.wg, [
        {
          ...verified(t, t.wg, 3, {}, null),
          status: 'unverifiable',
          code: 'signature_invalid',
          header: null,
          event: { header: { id: 'named' } },
        },
      ]),
    );
    expect(screen.getByTestId('task.agent.name')).toHaveTextContent('Unverifiable name (signature_invalid)');
  });

  it('opens the rename field empty while the name is not readable', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    header(t, taskRow(t, { name_event_id: 'named' }) as TaskRow);
    fireEvent.click(screen.getByTestId('task.agent.rename'));
    expect(screen.getByTestId('task.agent.rename.input')).toHaveValue('');
  });
});

describe('labels and sessions tab', () => {
  it('render a label with custom test IDs', async () => {
    const t = await taskWorld();
    render(
      wrap(t, <AgentName row={taskRow(t, { number: 7 }) as TaskRow} ids={{ root: 'r', name: 'n', handle: 'h' }} />),
    );
    expect(screen.getByTestId('r')).toHaveTextContent('Loading the name…#7');
  });

  it('shows the handles, the identity of a child with its own and the purpose of a subagent', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    const task = taskRow(t, { task_id: T1, number: 5 }) as TaskRow;
    putTasks(t.store, [task]);
    putSession(t.store, sessionRow(t, { session_id: 'm', task_id: T1 }) as SessionRow, [], 0);
    putSession(
      t.store,
      sessionRow(t, {
        session_id: 'c',
        task_id: T1,
        parent_session_id: 'm',
        depth: 1,
        agent_path: '3',
        created_at: at(1),
        github_identity: IDENTITY,
        origin_operation_id: '0190f000-0000-7000-8000-0000000000b1',
      }) as SessionRow,
      [],
      0,
    );
    putSession(
      t.store,
      sessionRow(t, {
        session_id: 'c2',
        task_id: T1,
        parent_session_id: 'm',
        depth: 1,
        agent_path: '4',
        created_at: at(2),
        origin_operation_id: '0190f000-0000-7000-8000-0000000000b2',
      }) as SessionRow,
      [],
      0,
    );
    render(wrap(t, <MemoryRouter>{<SessionsTab orgId={t.org} taskId={T1} />}</MemoryRouter>));
    const nodes = screen.getAllByTestId('sessions.node');
    expect(nodes.map((node) => node.dataset.handle)).toEqual(['#5', '#5.3', '#5.4']);
    // a subagent with the task's identity names none
    expect(within(nodes[2]!).queryByTestId('sessions.identity')).toBeNull();
    expect(within(nodes[0]!).getAllByTestId('sessions.link')[0]).toHaveTextContent('Main session #5');
    expect(within(nodes[1]!).getByTestId('sessions.link')).toHaveTextContent('Subagent #5.3');
    expect(within(nodes[1]!).getByTestId('sessions.identity')).toHaveTextContent('acts as another account');
    await waitFor(() =>
      expect(t.client.of(`GET /v1/scopes/${t.wg}/events`)[0]!.target).toContain('kinds=operation.params'),
    );
  });

  it('shows the sessions without handles before the task row is known', async () => {
    const t = await taskWorld();
    putSession(t.store, sessionRow(t, { session_id: 'm', task_id: T1 }) as SessionRow, [], 0);
    render(wrap(t, <MemoryRouter>{<SessionsTab orgId={t.org} taskId={T1} />}</MemoryRouter>));
    expect(screen.getByTestId('sessions.node')).toHaveAttribute('data-handle', '');
    expect(screen.getByTestId('sessions.link')).toHaveTextContent(/^Main session/);
  });
});
