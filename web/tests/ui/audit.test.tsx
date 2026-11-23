// U-WEB-ui-audit (screen and task tab): rows with names (members, device labels, workgroups, task titles), results,
// targets, details and event links; the filters (URL state, validation, apply, reset, a URL from elsewhere), paging,
// load and error states; the chain verification for admins only; the task tab with the task fixed – in jsdom against
// the org world.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { fromLocalInput, localInput } from '../../src/ui/screens/audit/AuditView';
import { AuditScreen } from '../../src/ui/screens/audit/AuditScreen';
import { AuditTab } from '../../src/ui/task-tabs/audit/AuditTab';
import { tab } from '../../src/ui/task-tabs/audit';
import { setAgents } from '../../src/core/store/slices/agents';
import { putEvents } from '../../src/core/store/slices/events';
import { HubError } from '../../src/core/net/signedFetch';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { idOf } from '../core/log/world';
import { auditRow, iso, T0 } from '../core/monitoring/fixtures';
import { mount, names } from './org-world';

const TASK = '01930000-0000-7000-8000-00000000c001';
const TASK2 = '01930000-0000-7000-8000-00000000c002';
const SESSION = '01930000-0000-7000-8000-00000000e001';
const UNKNOWN_WG = '01930000-0000-7000-8000-00000000a0ff';

async function world(as: 'ada' | 'bob' = 'ada') {
  const one = await idOf('wg one');
  const w = await orgWorld({
    as,
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.admitDevice(a.ada, a.dev1, [world.defaultWg]);
      await world.admitDevice(a.ada, a.dev2, [world.defaultWg]);
    },
  });
  w.hub.now = T0 + 60_000;
  names(w, { [w.others.ada.id]: 'Ada' }, { [w.world.defaultWg]: 'Default' });
  const wg = w.world.defaultWg;
  putEvents(w.store, w.org, [
    {
      org: w.org,
      scopeId: w.org,
      scopeSeq: 40,
      receivedAt: null,
      event: {},
      status: 'verified',
      code: null,
      header: { kind: 'device.profile', ref: w.others.dev1.id, author: w.others.ada.id, id: 'label-1' },
      body: { label: 'Mac Studio' },
    },
  ]);
  putEvents(w.store, wg, [
    {
      org: w.org,
      scopeId: wg,
      scopeSeq: 1,
      receivedAt: null,
      event: {},
      status: 'verified',
      code: null,
      header: { kind: 'task.meta', id: 'title-1', task: TASK },
      body: { title: 'Fix the build' },
    },
  ]);
  return { w, one, wg };
}

function rowsOf(w: OrgWorld, wg: string) {
  const { ada, dev1, dev2 } = w.others;
  return [
    auditRow(9, T0 + 9000, {
      actor_id: ada.id,
      action: 'event.append',
      wg_id: wg,
      task_id: TASK,
      session_id: SESSION,
      event_id: 'e1',
      target_kind: 'event',
      target_id: 'e1',
      detail: { kind: 'session.output', scope_seq: 4 },
    }),
    auditRow(8, T0 + 8000, {
      actor_id: dev1.id,
      actor_kind: 'device',
      action: 'operation.failed',
      result: 'failed',
      device_id: dev1.id,
      wg_id: UNKNOWN_WG,
      task_id: TASK2,
    }),
    auditRow(7, T0 + 7000, {
      actor_id: null,
      actor_kind: 'hub',
      action: 'invite.expire',
      target_kind: 'invite',
      target_id: 'inv-1',
    }),
    auditRow(6, T0 + 6000, { actor_id: w.others.bob.id, action: 'log.grant', target_kind: 'user', target_id: ada.id }),
    auditRow(5, T0 + 5000, {
      actor_id: dev2.id,
      actor_kind: 'device',
      action: 'device.connect',
      device_id: dev2.id,
      target_kind: 'device',
      target_id: null,
    }),
  ];
}

function mountScreen(w: OrgWorld, search = '') {
  return mount(w, [{ path: '/o/:org/audit', element: <AuditScreen /> }], `/o/${w.org}/audit${search}`);
}

const row = (seq: number) => screen.getAllByTestId('audit.row').find((node) => node.dataset.seq === String(seq))!;

describe('audit view', () => {
  it('shows rows newest first with names, results, targets, details and links', async () => {
    const { w, wg } = await world();
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({ rows: rowsOf(w, wg), next_before: null }));
    mountScreen(w);
    expect(await screen.findAllByTestId('audit.row')).toHaveLength(5);
    const first = within(row(9));
    expect(first.getByTestId('audit.row.time')).toHaveTextContent(
      /^\d{2}:\d{2}:\d{2}\.\d{3}$|\d{2}:\d{2}:\d{2}\.\d{3}$/,
    );
    expect(first.getByTestId('audit.row.actor')).toHaveTextContent('Ada');
    expect(first.getByTestId('audit.row.result')).toHaveTextContent('OK');
    expect(first.getByTestId('audit.row.target')).toHaveTextContent('Target: event e1');
    expect(first.getByTestId('audit.row.wg')).toHaveTextContent('Workgroup: Default');
    expect(first.getByTestId('audit.row.task')).toHaveTextContent('Fix the build');
    expect(first.getByTestId('audit.row.event')).toHaveAttribute('href', `/o/${w.org}/s/${SESSION}`);
    expect(first.getByTestId('audit.row.detail')).toHaveTextContent('kind=session.output · scope_seq=4');
    const second = within(row(8));
    expect(second.getByTestId('audit.row.actor')).toHaveTextContent('Mac Studio');
    expect(second.getByTestId('audit.row.result')).toHaveAttribute('data-result', 'failed');
    expect(second.getByTestId('audit.row.device')).toHaveTextContent('Device: Mac Studio');
    expect(second.getByTestId('audit.row.wg')).toHaveTextContent(`Workgroup: Workgroup …${UNKNOWN_WG.slice(-6)}`);
    expect(second.getByTestId('audit.row.task')).toHaveTextContent(`Task …${TASK2.slice(-6)}`);
    expect(second.queryByTestId('audit.row.event')).toBeNull();
    expect(second.queryByTestId('audit.row.detail')).toBeNull();
    expect(second.queryByTestId('audit.row.target')).toBeNull();
    expect(within(row(7)).getByTestId('audit.row.actor')).toHaveTextContent('Hub');
    expect(within(row(7)).getByTestId('audit.row.target')).toHaveTextContent('Target: invite inv-1');
    expect(within(row(6)).getByTestId('audit.row.actor')).toHaveTextContent(`User …${w.others.bob.id.slice(-6)}`);
    expect(within(row(6)).getByTestId('audit.row.target')).toHaveTextContent('Target: Ada');
    const fifth = within(row(5));
    expect(fifth.getByTestId('audit.row.actor')).toHaveTextContent(`Device …${w.others.dev2.id.slice(-6)}`);
    expect(fifth.getByTestId('audit.row.device')).toHaveTextContent(`Device: ${w.others.dev2.id.slice(-6)}`);
    expect(fifth.getByTestId('audit.row.target')).toHaveTextContent('Target: device');
    // the filter options: actors (users and devices of the log) and workgroups by name
    const actors = within(screen.getByTestId('audit.filter.actor'))
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(actors).toEqual(
      expect.arrayContaining(['Anyone', 'Ada', 'Mac Studio', `User …${w.others.bob.id.slice(-6)}`]),
    );
    const wgs = within(screen.getByTestId('audit.filter.wg'))
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(wgs).toContain('Default');
  });

  it('validates, applies and resets the filters through the URL', async () => {
    const { w, wg } = await world();
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({ rows: rowsOf(w, wg), next_before: null }));
    const { router } = mountScreen(w, `?from=${encodeURIComponent(iso(T0 + 6500))}`);
    // the time filter is the client's: rows before `from` are not shown
    await waitFor(() => expect(screen.getAllByTestId('audit.row').map((n) => n.dataset.seq)).toEqual(['9', '8', '7']));
    expect((screen.getByTestId('audit.filter.from') as HTMLInputElement).value).toBe(localInput(T0 + 6000));
    const field = (name: string) => screen.getByTestId(`audit.filter.${name}`);
    fireEvent.change(field('action'), { target: { value: 'Not Valid' } });
    fireEvent.change(field('task'), { target: { value: 'nope' } });
    fireEvent.change(field('session'), { target: { value: 'nope' } });
    expect(screen.getByTestId('audit.filter.invalid')).toBeInTheDocument();
    expect(field('action')).toHaveAttribute('aria-invalid', 'true');
    expect(field('apply')).toBeDisabled();
    fireEvent.submit(screen.getByTestId('audit.filters'));
    expect(router.state.location.search).toBe(`?from=${encodeURIComponent(iso(T0 + 6500))}`);
    fireEvent.change(field('action'), { target: { value: 'operation' } });
    fireEvent.change(field('task'), { target: { value: TASK } });
    fireEvent.change(field('session'), { target: { value: SESSION } });
    fireEvent.change(field('actor'), { target: { value: w.others.ada.id } });
    fireEvent.change(field('wg'), { target: { value: wg } });
    fireEvent.change(field('from'), { target: { value: '' } });
    fireEvent.change(field('to'), { target: { value: localInput(T0 + 9000) } });
    fireEvent.click(field('apply'));
    const query = new URLSearchParams(router.state.location.search);
    expect(Object.fromEntries(query)).toEqual({
      action: 'operation',
      actor: w.others.ada.id,
      wg,
      task: TASK,
      session: SESSION,
      to: iso(T0 + 9999),
    });
    const last = w.client.of(`GET /v1/orgs/${w.org}/audit`).at(-1)!;
    expect(last.target).toContain('action=operation');
    expect(last.target).not.toContain('to=');
    // a URL from elsewhere replaces the draft
    await act(async () => {
      await router.navigate(`/o/${w.org}/audit?action=log`);
    });
    expect((field('action') as HTMLInputElement).value).toBe('log');
    expect((field('task') as HTMLInputElement).value).toBe('');
    fireEvent.click(field('reset'));
    expect(router.state.location.search).toBe('');
    expect((field('action') as HTMLInputElement).value).toBe('');
  });

  it('pages, and shows the load, error and empty states', async () => {
    const { w, wg } = await world();
    let answer: (target: string) => unknown = () => new Promise(() => undefined);
    w.client.on(`GET /v1/orgs/${w.org}/audit`, (_body, target) => answer(target));
    const { router } = mountScreen(w);
    expect(await screen.findByTestId('audit.loading')).toBeInTheDocument();
    expect(screen.queryByTestId('audit.empty')).toBeNull();
    answer = (target) =>
      target.includes('before=')
        ? { rows: rowsOf(w, wg).slice(3), next_before: null }
        : { rows: rowsOf(w, wg).slice(0, 3), next_before: 'c1' };
    await act(async () => {
      await router.navigate(`/o/${w.org}/audit?action=event`);
    });
    expect(await screen.findAllByTestId('audit.row')).toHaveLength(3);
    fireEvent.click(screen.getByTestId('audit.more'));
    await waitFor(() => expect(screen.getAllByTestId('audit.row')).toHaveLength(5));
    expect(screen.queryByTestId('audit.more')).toBeNull();
    answer = () => {
      throw new HubError('not_found', 404);
    };
    await act(async () => {
      await router.navigate(`/o/${w.org}/audit?action=log`);
    });
    expect(await screen.findByTestId('audit.error')).toHaveTextContent('(not_found)');
    expect(screen.getByTestId('audit.empty')).toBeInTheDocument();
  });

  it('marks rows written by an agent: the name of an own agent, else "by agent" (11-agent-access.md section 7)', async () => {
    const { w } = await world();
    const own = '01a1ee56-5e00-7242-9e24-f87629ad2977';
    const foreign = '01a1ee56-5e00-79bc-ad68-1fafbfc5d917';
    w.client.on('GET /v1/me/agents', () => ({ agents: [] }));
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({
      rows: [
        { ...auditRow(3, T0 + 3000, { action: 'read.org' }), agent_id: own },
        { ...auditRow(2, T0 + 2000, { action: 'read.org' }), agent_id: foreign },
        auditRow(1, T0 + 1000, { action: 'read.org' }),
      ],
      next_before: null,
    }));
    mountScreen(w);
    expect(await screen.findAllByTestId('audit.row')).toHaveLength(3);
    act(() =>
      setAgents(w.store, [
        {
          agentId: own,
          state: 'active',
          label: { name: 'Claude Code', host: 'Mac' },
          createdAt: iso(T0),
          confirmedAt: iso(T0),
          claimedAt: null,
          lastUsedAt: null,
          revokedAt: null,
          replaces: null,
        },
      ]),
    );
    expect(within(row(3)).getByTestId('audit.row.agent')).toHaveTextContent('by agent Claude Code');
    expect(within(row(2)).getByTestId('audit.row.agent')).toHaveTextContent('by agent');
    expect(within(row(1)).queryByTestId('audit.row.agent')).toBeNull();
  });

  it('verifies the chain for admins: intact, broken, failed', async () => {
    const { w } = await world();
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({ rows: [], next_before: null }));
    const head = { seq: 12, hash: 'ab'.repeat(32) };
    let answer: () => unknown = () => ({
      ok: true,
      head,
      checkpoint: { hub_key_id: 'hub-1', seq: 10, hash: 'cd'.repeat(32), published_at: iso(T0), signature: 's' },
    });
    w.client.on(`GET /v1/orgs/${w.org}/audit/verify`, () => answer());
    mountScreen(w);
    fireEvent.click(await screen.findByTestId('audit.verify.run'));
    const result = await screen.findByTestId('audit.verify.result');
    expect(result).toHaveAttribute('data-ok', 'true');
    expect(result).toHaveTextContent('The audit chain is intact.');
    expect(screen.getByTestId('audit.verify.head')).toHaveTextContent(`Head: row 12, hash ${'ab'.repeat(8)}…`);
    expect(screen.getByTestId('audit.verify.checkpoint')).toHaveTextContent(/row 10, published .* by key hub-1/);
    answer = () => new Promise(() => undefined);
    fireEvent.click(screen.getByTestId('audit.verify.run'));
    expect(screen.getByTestId('audit.verify.run')).toHaveTextContent('Verifying…');
    expect(screen.getByTestId('audit.verify.run')).toBeDisabled();
    answer = () => ({ ok: false, head, checkpoint: null });
    act(() => w.store.update('audit', (state) => ({ ...state, verify: {} })));
    fireEvent.click(screen.getByTestId('audit.verify.run'));
    await waitFor(() => expect(screen.getByTestId('audit.verify.result')).toHaveAttribute('data-ok', 'false'));
    expect(screen.getByTestId('audit.verify.result')).toHaveTextContent('The audit chain is broken');
    expect(screen.getByTestId('audit.verify.checkpoint')).toHaveTextContent('No checkpoint published yet.');
    answer = () => {
      throw new HubError('forbidden', 403);
    };
    fireEvent.click(screen.getByTestId('audit.verify.run'));
    expect(await screen.findByTestId('audit.verify.error')).toHaveTextContent('(forbidden)');
  });

  it('shows no verification to members and works before the log is verified', async () => {
    const { w } = await world('bob');
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({ rows: [], next_before: null }));
    mountScreen(w);
    expect(await screen.findByTestId('audit.empty')).toBeInTheDocument();
    expect(screen.queryByTestId('audit.verify')).toBeNull();
  });

  it('works for an org whose log is not verified yet', async () => {
    const { w } = await world();
    w.client.on('GET /v1/orgs/other-org/audit', () => ({ rows: [], next_before: null }));
    mount(w, [{ path: '/o/:org/audit', element: <AuditScreen /> }], '/o/other-org/audit');
    expect(await screen.findByTestId('audit.empty')).toBeInTheDocument();
    expect(within(screen.getByTestId('audit.filter.actor')).getAllByRole('option')).toHaveLength(1);
    expect(screen.queryByTestId('audit.verify')).toBeNull();
  });
});

describe('audit task tab', () => {
  it('fixes the task filter and keeps the tab on apply', async () => {
    const { w, wg } = await world();
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({ rows: rowsOf(w, wg).slice(0, 1), next_before: null }));
    expect(tab).toMatchObject({ id: 'audit', order: 50, labelKey: 'audit.tab' });
    const element = <AuditTab orgId={w.org} taskId={TASK} />;
    const { router } = mount(w, [{ path: '/o/:org/t/:task', element }], `/o/${w.org}/t/${TASK}?tab=audit`);
    expect(await screen.findAllByTestId('audit.row')).toHaveLength(1);
    expect(screen.queryByTestId('audit.filter.task')).toBeNull();
    expect(w.client.of(`GET /v1/orgs/${w.org}/audit`)[0]!.target).toContain(`task=${TASK}`);
    fireEvent.change(screen.getByTestId('audit.filter.action'), { target: { value: 'event' } });
    fireEvent.click(screen.getByTestId('audit.filter.apply'));
    expect(router.state.location.search).toBe('?action=event&tab=audit');
    fireEvent.click(screen.getByTestId('audit.filter.reset'));
    expect(router.state.location.search).toBe('?tab=audit');
    await waitFor(() => expect(w.client.of(`GET /v1/orgs/${w.org}/audit`).at(-1)!.target).toContain(`task=${TASK}`));
  });
});

describe('time inputs', () => {
  it('converts between unix ms and datetime-local values', () => {
    expect(localInput(null)).toBe('');
    const value = localInput(T0);
    expect(fromLocalInput(value)).toBe(T0);
    expect(fromLocalInput('')).toBeNull();
    expect(fromLocalInput('not a time')).toBeNull();
  });
});
