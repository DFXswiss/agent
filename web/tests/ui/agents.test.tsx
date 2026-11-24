// "Connected agents" in the settings and its "Security activity" (06-web.md section 8.3, 11-agent-access.md sections
// 5.3 and 7) in jsdom against a scripted client: loading, failure and retry, the main orchestrator and the earlier
// agents with opened labels (unknown agent, the replaced agent named when known, never used), no main orchestrator,
// the `?agent=` hint, the dialogs of "Disconnect" and "Disconnect all agents" with a refused attempt, and the account
// audit with its agent markers.
import { act, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { sealLabel } from '../../src/core/crypto/agent';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { I18nProvider } from '../../src/ui/i18n';
import { AgentsSection, section } from '../../src/ui/settings/sections/agents';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { AGENT, agentWorld, type Handler, OTHER, USER } from '../core/agents/world';

// the passkey flows run real WebCrypto: allow them time on a loaded machine
configure({ asyncUtilTimeout: 10_000 });

const T = '2026-11-23T12:00:00.000Z';
const THIRD = '01a1ee56-5e00-7aaa-8d68-1fafbfc5d917';

type World = Awaited<ReturnType<typeof agentWorld>>;

function mount(w: World, element: ReactNode, path = '/settings') {
  const router = createMemoryRouter([{ path: '/settings', element }], { initialEntries: [path] });
  const app = { store: w.store, tasks: { ...w.context, now: () => Date.parse(T) }, client: w.client } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
}

const auditRow = (seq: number, agent_id?: string) => ({
  seq,
  at: T,
  actor_id: USER,
  actor_kind: 'user',
  action: 'agent.confirm',
  result: 'ok',
  org_id: null,
  wg_id: null,
  task_id: null,
  session_id: null,
  device_id: null,
  target_kind: null,
  target_id: null,
  detail: {},
  event_id: null,
  ...(agent_id !== undefined && { agent_id }),
});

async function world(routes: Record<string, Handler>) {
  return agentWorld((method, target, body) => {
    const key = `${method} ${target.split('?')[0]}`;
    const handler = routes[key];
    if (handler === undefined) throw new HubError('not_found', 404);
    return handler(method, target, body);
  });
}

describe('AgentsSection', () => {
  it('is registered in the settings', () => {
    expect(section).toMatchObject({ id: 'agents', titleKey: 'agents.title' });
  });

  it('lists the agents, disconnects one and all, and shows the account audit', async () => {
    let agents: unknown[] = [];
    let failRevoke = true;
    const w = await world({
      'GET /v1/me/agents': () => ({ agents }),
      [`DELETE /v1/me/agents/${AGENT}`]: () => {
        if (failRevoke) {
          failRevoke = false;
          throw new HubError('unavailable', 503);
        }
        return {};
      },
      'DELETE /v1/me/agents': () => ({ revoked: 1 }),
      'GET /v1/me/audit': (_m, target) =>
        target.includes('before=')
          ? { rows: [auditRow(1, THIRD)], next_before: null }
          : { rows: [auditRow(3), auditRow(2, AGENT)], next_before: 'c' },
    });
    const kem = w.identity.kemPublic();
    const base = { created_at: T, confirmed_at: T, claimed_at: T, revoked_at: null, last_used_at: T };
    agents = [
      {
        ...base,
        agent_id: AGENT,
        state: 'active',
        label_ct: await sealLabel(kem, AGENT, USER, { name: 'Claude Code', host: 'Mac' }),
        replaces: THIRD,
      },
      {
        ...base,
        agent_id: OTHER,
        state: 'confirmed',
        label_ct: await sealLabel(kem, OTHER, 'someone-else', { name: 'x', host: 'y' }),
        replaces: null,
        last_used_at: null,
      },
      {
        ...base,
        agent_id: THIRD,
        state: 'expired',
        label_ct: await sealLabel(kem, THIRD, USER, { name: 'Helper', host: 'Mac' }),
        replaces: OTHER,
      },
    ];
    mount(w, <AgentsSection />, '/settings?agent=failed');
    expect(screen.getByTestId('agents.hint.failed')).toBeInTheDocument();
    expect(screen.getByTestId('agents.loading')).toBeInTheDocument();
    const items = await screen.findAllByTestId('agents.item');
    expect(items.map((item) => within(item).getByTestId('agents.item.name').textContent)).toEqual([
      'Claude Code',
      'Unknown agent',
      'Helper',
    ]);
    // the newest live agent is the main orchestrator; the rest is history (a live leftover can still be disconnected)
    expect(within(screen.getByTestId('agents.main')).getByTestId('agents.item.name')).toHaveTextContent('Claude Code');
    expect(screen.getByTestId('agents.main')).toHaveTextContent('Main orchestrator');
    expect(within(screen.getByTestId('agents.history')).getAllByTestId('agents.item')).toEqual([items[1], items[2]]);
    expect(within(items[0]!).getByTestId('agents.item.replaces')).toHaveTextContent('Replaced Helper');
    expect(within(items[1]!).queryByTestId('agents.item.replaces')).toBeNull();
    expect(within(items[1]!).getByTestId('agents.item.last-used')).toHaveTextContent('Not used yet');
    expect(within(items[1]!).queryByTestId('agents.item.host')).toBeNull();
    expect(within(items[2]!).queryByTestId('agents.item.replaces')).toBeNull();
    expect(within(items[2]!).queryByTestId('agents.item.disconnect')).toBeNull();
    expect(within(items[1]!).getByTestId('agents.item.state')).toHaveTextContent('Waiting for the agent');

    // the account audit: by you, by a known agent, "load more", by an unknown agent
    const who = await screen.findAllByTestId('agents.activity.row.who');
    expect(who.map((item) => item.textContent)).toEqual(['by you', 'by agent Claude Code']);
    fireEvent.click(screen.getByTestId('agents.activity.more'));
    await waitFor(() => expect(screen.getAllByTestId('agents.activity.row.who')).toHaveLength(3));
    expect(screen.getAllByTestId('agents.activity.row.who')[2]).toHaveTextContent('by agent');
    expect(screen.queryByTestId('agents.activity.more')).toBeNull();

    // disconnect one: refused first, then done; the unknown agent's dialog names it so
    fireEvent.click(within(items[1]!).getByTestId('agents.item.disconnect'));
    expect(screen.getByTestId('agents.dialog')).toHaveTextContent('Disconnect Unknown agent?');
    fireEvent.click(screen.getByTestId('agents.dialog.cancel'));
    fireEvent.click(within(items[0]!).getByTestId('agents.item.disconnect'));
    expect(screen.getByTestId('agents.dialog')).toHaveTextContent('Disconnect Claude Code?');
    fireEvent.click(screen.getByTestId('agents.dialog.confirm'));
    expect(await screen.findByTestId('agents.dialog.failed')).toHaveAttribute('data-code', 'unavailable');
    fireEvent.click(screen.getByTestId('agents.dialog.confirm'));
    await waitFor(() => expect(screen.queryByTestId('agents.dialog')).toBeNull());

    // disconnect all
    fireEvent.click(screen.getByTestId('agents.disconnect-all'));
    expect(screen.getByTestId('agents.dialog.body')).toHaveTextContent('Every agent loses all access at once.');
    agents = [];
    fireEvent.click(screen.getByTestId('agents.dialog.confirm'));
    expect(await screen.findByTestId('agents.empty')).toHaveTextContent('No AI agent is connected.');
    expect(screen.queryByTestId('agents.disconnect-all')).toBeNull();
  });

  it('shows failures with a retry and an empty account audit', async () => {
    let fail = true;
    const w = await world({
      'GET /v1/me/agents': () => {
        if (fail) throw new HubError('unavailable', 503);
        return { agents: [] };
      },
      'GET /v1/me/audit': () => ({ rows: [], next_before: null }),
    });
    mount(w, <AgentsSection />, '/settings?agent=connected');
    expect(screen.getByTestId('agents.hint.connected')).toBeInTheDocument();
    expect(await screen.findByTestId('agents.load-failed')).toHaveTextContent('The agents could not be loaded');
    fail = false;
    await act(async () => fireEvent.click(screen.getByTestId('agents.retry')));
    expect(await screen.findByTestId('agents.empty')).toBeInTheDocument();
    expect(await screen.findByTestId('agents.activity.empty')).toBeInTheDocument();
  });

  it('shows the history without a main orchestrator', async () => {
    let agents: unknown[] = [];
    const w = await world({
      'GET /v1/me/agents': () => ({ agents }),
      'GET /v1/me/audit': () => ({ rows: [], next_before: null }),
    });
    const kem = w.identity.kemPublic();
    const base = { created_at: T, confirmed_at: T, claimed_at: T, last_used_at: T, revoked_at: T, state: 'revoked' };
    agents = [
      {
        ...base,
        agent_id: AGENT,
        label_ct: await sealLabel(kem, AGENT, USER, { name: 'Helper', host: 'Mac' }),
        replaces: OTHER,
      },
      {
        ...base,
        agent_id: OTHER,
        label_ct: await sealLabel(kem, OTHER, USER, { name: 'Claude Code', host: 'Mac' }),
        replaces: null,
      },
    ];
    mount(w, <AgentsSection />);
    expect(await screen.findByTestId('agents.main.none')).toHaveTextContent('No main orchestrator is connected.');
    const items = within(screen.getByTestId('agents.history')).getAllByTestId('agents.item');
    expect(within(items[0]!).getByTestId('agents.item.replaces')).toHaveTextContent('Replaced Claude Code');
    expect(screen.queryByTestId('agents.disconnect-all')).toBeNull();
  });

  it('windows a long account audit and pages with the cursor (ADR-063 section 1)', async () => {
    let answer: (target: string) => unknown = (target) =>
      target.includes('before=')
        ? { rows: Array.from({ length: 30 }, (_, i) => auditRow(30 - i)), next_before: null }
        : { rows: Array.from({ length: 120 }, (_, i) => auditRow(1000 - i)), next_before: 'c1' };
    const w = await world({
      'GET /v1/me/agents': () => ({ agents: [] }),
      'GET /v1/me/audit': (_method, target) => answer(target),
    });
    mount(w, <AgentsSection />);
    const rows = () => screen.getAllByTestId('agents.activity.row');
    await waitFor(() => expect(rows()).toHaveLength(100));
    const list = screen.getByTestId('agents.activity.rows');
    expect(list.closest('[data-list]')).toHaveAttribute('data-list', 'settings.agents.activity');
    expect(screen.queryByTestId('agents.activity.more')).toBeNull();
    fireEvent.click(screen.getByTestId('agents.activity.rows.older'));
    expect(rows()).toHaveLength(120);
    // a page that is still on its way disables the button
    const release: { done: () => void } = { done: () => undefined };
    const first = answer;
    answer = (target) => new Promise((resolve) => (release.done = () => resolve(first(target))));
    fireEvent.click(screen.getByTestId('agents.activity.more'));
    await waitFor(() => expect(screen.getByTestId('agents.activity.more')).toBeDisabled());
    await act(async () => release.done());
    await waitFor(() => expect(rows()).toHaveLength(150));
    expect(screen.queryByTestId('agents.activity.more')).toBeNull();
  });

  it('reports an account audit that does not load', async () => {
    const w = await world({ 'GET /v1/me/agents': () => ({ agents: [] }) });
    mount(w, <AgentsSection />, '/settings?agent=other');
    expect(await screen.findByTestId('agents.activity.load-failed')).toHaveAttribute('data-code', 'not_found');
    expect(screen.queryByTestId('agents.hint.other')).toBeNull();
  });
});
