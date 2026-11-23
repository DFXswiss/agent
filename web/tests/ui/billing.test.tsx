// U-WEB-billing (KOR-31): the section "Credit and usage", the credit badge in the header and the task tab "Costs".
import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { BillingSection, section } from '../../src/ui/settings/sections/billing';
import { CreditBadge, headerItem, REFRESH_MS } from '../../src/ui/shell/header/billing';
import { ChargesTab } from '../../src/ui/task-tabs/charges/ChargesTab';
import { tab } from '../../src/ui/task-tabs/charges';
import { BILLING, totals } from '../core/billing/world';
import { credWorld } from '../core/credentials/world';
import { mount } from './chat-world';

afterEach(() => {
  vi.useRealTimers();
});

describe('registration', () => {
  it('registers the section, the header item and the task tab', () => {
    expect(section).toMatchObject({ id: 'billing', order: 66, titleKey: 'billing.title' });
    expect(headerItem).toMatchObject({ id: 'credit', order: 15 });
    expect(tab).toMatchObject({ id: 'charges', order: 35, labelKey: 'billing.tab' });
  });
});

describe('credit and usage', () => {
  it('shows balance, prices, history and usage, a notice when used up, failures and reloads', async () => {
    const t = await credWorld();
    let answer: () => unknown = () => BILLING;
    t.client.on(`GET /v1/orgs/${t.org}/billing`, () => answer());
    // without an active org nothing is read
    mount(t, [{ path: '/settings', element: <BillingSection /> }], '/settings');
    expect(screen.getByTestId('billing.loading')).toBeVisible();
    act(() => setActiveOrg(t.store, t.org));
    expect(await screen.findByTestId('billing.balance')).toHaveTextContent('Balance: $4.55');
    expect(screen.getByTestId('billing.prices')).toHaveTextContent('deepseek/flash: $0.60 per million input tokens');
    expect(screen.getByTestId('billing.prices')).toHaveTextContent('VM large: $0.72 per hour');
    expect(screen.getByTestId('billing.credits')).toHaveTextContent(
      'Credit by the operator: $2.00 (balance $7.00) – hi',
    );
    expect(screen.getByTestId('billing.credits')).toHaveTextContent('Start credit: $5.00');
    expect(screen.getByTestId('billing.days')).toHaveTextContent('2026-11-21: $0.45 (model $0.45, runtime $0.0030)');
    expect(screen.getByTestId('billing.workgroups')).toHaveTextContent('wg-1 (30 s)');
    expect(screen.queryByTestId('billing.exhausted')).toBeNull();
    answer = () => ({
      ...BILLING,
      exhausted: true,
      billing_active: false,
      price_list: null,
      credits: [],
      days: [],
      workgroups: [],
    });
    fireEvent.click(screen.getByTestId('billing.reload'));
    expect(await screen.findByTestId('billing.exhausted')).toHaveTextContent('paused until the operator adds credit');
    expect(screen.getByTestId('billing.inactive')).toBeVisible();
    expect(screen.getByTestId('billing.credits.empty')).toBeVisible();
    expect(screen.getByTestId('billing.days.empty')).toBeVisible();
    expect(screen.queryByTestId('billing.prices')).toBeNull();
    answer = () => {
      throw new HubError('not_found', 404);
    };
    fireEvent.click(screen.getByTestId('billing.reload'));
    expect(await screen.findByTestId('billing.load-failed')).toHaveAttribute('data-code', 'not_found');
  });
});

describe('credit badge', () => {
  it('shows the balance, the used-up credit, nothing while inactive, and refreshes', async () => {
    const t = await credWorld();
    let answer: () => unknown = () => BILLING;
    t.client.on(`GET /v1/orgs/${t.org}/billing`, () => answer());
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mount(t, [{ path: '/', element: <CreditBadge /> }], '/');
    expect(screen.queryByTestId('credit.badge')).toBeNull();
    act(() => setActiveOrg(t.store, t.org));
    expect(await screen.findByTestId('credit.badge')).toHaveTextContent('Credit $4.55');
    answer = () => ({ ...BILLING, exhausted: true, balance_micro_usd: -10 });
    await act(async () => {
      vi.advanceTimersByTime(REFRESH_MS);
    });
    expect(await screen.findByTestId('credit.badge')).toHaveTextContent('Credit used up');
    expect(screen.getByTestId('credit.badge')).toHaveAttribute('data-exhausted', 'yes');
    answer = () => ({ ...BILLING, billing_active: false });
    await act(async () => {
      vi.advanceTimersByTime(REFRESH_MS);
    });
    await vi.waitFor(() => expect(screen.queryByTestId('credit.badge')).toBeNull());
    answer = () => {
      throw new HubError('not_found', 404);
    };
    await act(async () => {
      vi.advanceTimersByTime(REFRESH_MS);
    });
    expect(screen.queryByTestId('credit.badge')).toBeNull();
    act(() => setActiveOrg(t.store, null));
  });
});

describe('task costs', () => {
  it('shows the charges per session, nothing to show, failures and reloads', async () => {
    const t = await credWorld();
    let answer: () => unknown = () => ({
      sessions: [{ session_id: '0190f000-0000-7000-8000-00000000aaaa', ...totals }],
      total_micro_usd: 448_200,
    });
    t.client.on('GET /v1/tasks/task-1/charges', () => answer());
    mount(t, [{ path: '/', element: <ChargesTab orgId={t.org} taskId="task-1" /> }], '/');
    expect(await screen.findByTestId('charges.total')).toHaveTextContent('Total $0.45');
    expect(screen.getByTestId('charges.sessions')).toHaveTextContent(
      'Session 0190f000: model $0.45 (1,100,000 tokens), runtime $0.0030 (30 s)',
    );
    answer = () => ({ sessions: [], total_micro_usd: 0 });
    fireEvent.click(screen.getByTestId('charges.reload'));
    expect(await screen.findByTestId('charges.empty')).toBeVisible();
    answer = () => {
      throw new HubError('unavailable', 503);
    };
    fireEvent.click(screen.getByTestId('charges.reload'));
    expect(await screen.findByTestId('charges.load-failed')).toHaveAttribute('data-code', 'unavailable');
  });
});
