// U-WEB-ui-model-access (KOR-50, ADR-081): the two fallback settings in the section "Model access", the section "Model
// access and quotas" of the active organization (own subscription with windows or "not readable" and the last observed
// limit, the operator's subscriptions, DFX access, the summary) and the model switch line of the task view.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { formatClock, localTimeZone } from '../../src/core/i18n';
import { applyFrame, frameHandlers } from '../../src/core/net/frames';
import { HubError } from '../../src/core/net/signedFetch';
import { setProfile } from '../../src/core/store/slices/me';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { putSession } from '../../src/core/store/slices/tasks';
import { ModelSection } from '../../src/ui/settings/sections/model';
import { ModelQuotasSection, section } from '../../src/ui/settings/sections/model-quotas';
import { ModelSwitchLine } from '../../src/ui/screens/task/ModelSwitchLine';
import { switchRow } from '../core/credentials/switch-rows';
import { sessionRow, taskRow, taskWorld } from '../core/tasks/world';
import { mount } from './chat-world';

const AT = '2026-11-24T13:05:00.000Z';
const LATER = '2026-11-24T14:05:00.000Z';
const clock = (value: string) =>
  formatClock(Date.parse(value), { locale: 'en', timeZone: localTimeZone(), now: Date.now() });

const subscription = (overrides: Record<string, unknown> = {}) => ({
  label: 'team-a',
  state: 'exhausted',
  readable: true,
  used_percent: 100,
  resets_at: LATER,
  limited_at: AT,
  until: LATER,
  fetched_at: AT,
  ...overrides,
});

const QUOTAS = {
  own: {
    windows: [
      {
        window: 'five_hour',
        used_percent: 100,
        resets_at: LATER,
        severity: 'critical',
        fetched_at: AT,
        device_id: '0190f000-0000-7000-8000-000000000001',
      },
      {
        window: 'weekly:opus',
        used_percent: 30,
        resets_at: null,
        severity: null,
        fetched_at: AT,
        device_id: '0190f000-0000-7000-8000-000000000001',
      },
    ],
    unreadable: null,
    limited: { at: AT, until: LATER, readable: true },
  },
  operator: {
    granted: true,
    subscriptions: [
      subscription(),
      subscription({ label: 'team-b', state: 'usable', used_percent: 40, limited_at: null, until: null }),
      subscription({
        label: 'team-c',
        state: 'blocked',
        readable: false,
        used_percent: null,
        resets_at: null,
        until: null,
      }),
      subscription({ label: 'team-d', state: 'later', used_percent: null, resets_at: null, limited_at: null }),
      subscription({ label: 'team-e', state: 'exhausted', until: null, resets_at: null, limited_at: null }),
    ],
  },
  platform: { available: true },
  summary: { usable: 1, exhausted: 2, next_release: LATER },
};

describe('fallback settings', () => {
  it('shows both settings of a current hub, saves them with the preference, none for an older hub', async () => {
    const t = await taskWorld();
    t.client.on('PUT /v1/me/settings', (body) => body);
    setProfile(t.store, {
      settings: {
        model_access: 'auto',
        model_id: null,
        locale: 'en',
        fallback: { own_to_dfx: false, operator_to_platform: true },
      },
    });
    mount(t, [{ path: '/settings', element: <ModelSection /> }], '/settings');
    const own = screen.getByTestId('model.fallback.own');
    const platform = screen.getByTestId('model.fallback.platform');
    expect(screen.getByTestId('model.fallback')).toHaveTextContent(
      'Use DFX access when my own subscription is used up',
    );
    expect(own).not.toBeChecked();
    expect(platform).toBeChecked();
    fireEvent.click(own);
    fireEvent.click(platform);
    fireEvent.click(screen.getByTestId('model.access.subscription'));
    expect(screen.getByTestId('model.fallback.own')).toBeChecked();
    fireEvent.click(screen.getByTestId('model.save'));
    expect(await screen.findByTestId('model.saved')).toBeVisible();
    expect(t.client.of('PUT /v1/me/settings')[0]!.body).toEqual({
      model_access: 'subscription',
      model_id: null,
      locale: 'en',
      fallback: { own_to_dfx: true, operator_to_platform: false },
    });
  });

  it('shows no fallback settings for a hub of an older release', async () => {
    const t = await taskWorld();
    setProfile(t.store, { settings: { model_access: 'auto', model_id: null, locale: 'en' } });
    mount(t, [{ path: '/settings', element: <ModelSection /> }], '/settings');
    expect(screen.getByTestId('model.form')).toBeVisible();
    expect(screen.queryByTestId('model.fallback')).toBeNull();
  });
});

describe('model access and quotas', () => {
  it('registers the section', () => {
    expect(section).toMatchObject({ id: 'model-quotas', order: 65, titleKey: 'model.org.title' });
  });

  it('shows the own and the operator subscriptions with state, share and release, DFX access and the summary', async () => {
    const t = await taskWorld();
    let answer: () => unknown = () => QUOTAS;
    t.client.on(`GET /v1/orgs/${t.org}/quotas`, () => answer());
    // without an active organization nothing is read
    mount(t, [{ path: '/settings', element: <ModelQuotasSection /> }], '/settings');
    expect(screen.queryByTestId('model-quotas.loading')).toBeNull();
    expect(t.client.of(`GET /v1/orgs/${t.org}/quotas`)).toHaveLength(0);
    act(() => setActiveOrg(t.store, t.org));
    expect(await screen.findByTestId('model-quotas.summary')).toHaveTextContent(
      `Subscriptions: 1 available · 2 used up – next release ${clock(LATER)}`,
    );
    const bars = screen.getByTestId('model-quotas.own.bars');
    expect(bars).toHaveTextContent(`5 hours: 100% used · release ${clock(LATER)}`);
    expect(bars).toHaveTextContent('Week, opus: 30% used');
    expect(screen.getByTestId('model-quotas.own.limited')).toHaveTextContent(
      `Last observed limit: ${clock(AT)} – free from ${clock(LATER)}`,
    );
    const rows = screen.getAllByTestId('model-quotas.subscription');
    expect(rows.map((row) => row.dataset.state)).toEqual(['exhausted', 'usable', 'blocked', 'later', 'exhausted']);
    const [a, b, c, d, e] = rows as [HTMLElement, HTMLElement, HTMLElement, HTMLElement, HTMLElement];
    expect(within(a).getByTestId('model-quotas.subscription.state')).toHaveTextContent('used up');
    expect(within(a).getByTestId('model-quotas.subscription.release')).toHaveTextContent(
      `Used up until ${clock(LATER)}`,
    );
    expect(within(a).getByTestId('model-quotas.subscription.meter')).toHaveAttribute('value', '1');
    expect(within(b).getByTestId('model-quotas.subscription.release')).toHaveTextContent(`release ${clock(LATER)}`);
    expect(within(b).queryByTestId('model-quotas.subscription.limited')).toBeNull();
    // not readable: said honestly, with the last observed limit time
    expect(within(c).getByTestId('model-quotas.subscription.unreadable')).toHaveTextContent('quota not readable');
    expect(within(c).getByTestId('model-quotas.subscription.limited')).toHaveTextContent(
      `Last observed limit: ${clock(AT)}`,
    );
    expect(within(d).getByTestId('model-quotas.subscription.state')).toHaveTextContent('state unknown');
    expect(within(d).queryByTestId('model-quotas.subscription.release')).toBeNull();
    expect(within(e).queryByTestId('model-quotas.subscription.release')).toBeNull();
    expect(screen.getByTestId('model-quotas.platform')).toHaveTextContent('DFX access over OpenRouter: available');
    // the own subscription not readable, no operator subscriptions, DFX access down
    answer = () => ({
      own: { windows: [], unreadable: { since: AT }, limited: null },
      operator: { granted: true, subscriptions: [] },
      platform: { available: false },
      summary: { usable: 0, exhausted: 0, next_release: null },
    });
    fireEvent.click(screen.getByTestId('model-quotas.reload'));
    expect(await screen.findByTestId('model-quotas.own.unreadable')).toHaveTextContent('Not readable');
    expect(screen.getByTestId('model-quotas.summary')).toHaveTextContent('Subscriptions: 0 available · 0 used up');
    expect(screen.queryByTestId('model-quotas.own.limited')).toBeNull();
    expect(screen.getByTestId('model-quotas.operator.empty')).toBeVisible();
    expect(screen.getByTestId('model-quotas.platform')).toHaveAttribute('data-available', 'no');
    // no own subscription, the organization does not use the operator's
    answer = () => ({
      own: { windows: [], unreadable: null, limited: null },
      operator: { granted: false, subscriptions: [] },
      platform: { available: false },
      summary: { usable: 0, exhausted: 0, next_release: null },
    });
    fireEvent.click(screen.getByTestId('model-quotas.reload'));
    expect(await screen.findByTestId('model-quotas.own.none')).toBeVisible();
    expect(screen.getByTestId('model-quotas.operator.not-granted')).toBeVisible();
    answer = () => {
      throw new HubError('not_found', 404);
    };
    fireEvent.click(screen.getByTestId('model-quotas.reload'));
    expect(await screen.findByTestId('model-quotas.load-failed')).toHaveAttribute('data-code', 'not_found');
  });

  it('says it is loading until the first answer', async () => {
    const t = await taskWorld();
    setActiveOrg(t.store, t.org);
    t.client.on(`GET /v1/orgs/${t.org}/quotas`, () => new Promise(() => undefined));
    mount(t, [{ path: '/settings', element: <ModelQuotasSection /> }], '/settings');
    expect(screen.getByTestId('model-quotas.loading')).toBeVisible();
  });
});

describe('model switch line', () => {
  const hello = (t: Awaited<ReturnType<typeof taskWorld>>) =>
    act(() => {
      applyFrame(t.store, 'hello', { heads: {}, hub_time: '2026-11-24T12:00:00Z' }, 0, frameHandlers());
    });

  it('shows the newest switch with its cause, reloads on a state change and a reconnect', async () => {
    const t = await taskWorld();
    const session = sessionRow(t, { state: 'working' });
    const row = taskRow(t, { main_session_id: session.session_id });
    let rows: unknown[] = [];
    t.client.on(`GET /v1/orgs/${t.org}/audit`, () => ({ rows, next_before: null }));
    mount(t, [{ path: '/t', element: <ModelSwitchLine row={row} /> }], '/t');
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/audit`)).toHaveLength(1));
    expect(screen.queryByTestId('task.model-switch')).toBeNull();
    rows = [switchRow(1)];
    act(() => putSession(t.store, { ...session, state: 'suspended' } as never, [], 0));
    expect(await screen.findByTestId('task.model-switch')).toHaveTextContent(
      `Model switched: Claude subscription → DFX access over OpenRouter (deepseek/flash) · used up until ${clock(LATER)}`,
    );
    const cases: [Record<string, unknown>, string][] = [
      [{ cause: 'exhausted', until: null, from_model: 'claude-opus' }, 'Claude subscription (claude-opus) → '],
      [{ cause: 'unusable', to_access: 'api_key', to_model: null }, '→ API key · former access could not be used'],
      [
        { cause: 'exhausted', until: null, from_access: 'operator', to_model: null },
        'operator subscription → DFX access',
      ],
      [
        { cause: 'released', from_access: 'platform', to_access: 'subscription', to_model: null },
        'subscription free again',
      ],
    ];
    for (const [detail, text] of cases) {
      rows = [switchRow(2, detail)];
      hello(t);
      await waitFor(() => expect(screen.getByTestId('task.model-switch')).toHaveTextContent(text));
    }
    expect(screen.getByTestId('task.model-switch')).toHaveAttribute('data-cause', 'released');
    // a failed read keeps what is shown
    t.client.on(`GET /v1/orgs/${t.org}/audit`, () => {
      throw new HubError('unavailable', 503);
    });
    hello(t);
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/audit`)).toHaveLength(7));
    expect(screen.getByTestId('task.model-switch')).toHaveAttribute('data-to', 'subscription');
    // a row outside the contract's lists is no switch
    t.client.on(`GET /v1/orgs/${t.org}/audit`, () => ({ rows: [switchRow(3, { cause: 'later' })], next_before: null }));
    hello(t);
    await waitFor(() => expect(screen.queryByTestId('task.model-switch')).toBeNull());
  });

  it('reads nothing of a session that is not known yet', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { main_session_id: null });
    t.client.on(`GET /v1/orgs/${t.org}/audit`, () => ({ rows: [], next_before: null }));
    mount(t, [{ path: '/t', element: <ModelSwitchLine row={row} /> }], '/t');
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/audit`)).toHaveLength(1));
    expect(screen.queryByTestId('task.model-switch')).toBeNull();
  });
});
