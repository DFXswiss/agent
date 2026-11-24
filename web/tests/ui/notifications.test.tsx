// U-WEB-ui-notifications: the notification center and the bell in jsdom against the core board world: list states
// (error with retry, empty, paging), client-built texts with fallbacks, unread markers, a click that marks read and
// navigates (also when marking fails), "Mark all read" and its failure, the badge from the profile and from live rows
// (99+), toasts that close by themselves after 6 s, with their button, or open their target.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { setProfile } from '../../src/core/store/slices/me';
import { receive } from '../../src/core/store/slices/notifications';
import { MAX_ROWS, NotificationsScreen, PAGE, WINDOW } from '../../src/ui/screens/notifications/NotificationsScreen';
import { Bell, headerItem, TOAST_MS } from '../../src/ui/shell/header/bell';
import { boardWorld } from '../core/whiteboards/world';
import { notification } from '../core/notifications/rows';
import { mount } from './chat-world';

afterEach(() => vi.useRealTimers());

const ROUTES = [{ path: '/notifications', element: <NotificationsScreen /> }];
const empty = { events: [], next_after: null, head: 0 };

async function world() {
  const w = await boardWorld();
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
  w.client.on(`GET /v1/scopes/${w.wg}/events`, () => empty);
  w.client.on('POST /v1/notifications/read', () => ({}));
  return w;
}

describe('notification center', () => {
  it('retries a failed load, then shows the empty list', async () => {
    const w = await world();
    w.client.on('GET /v1/notifications', () => {
      throw new HubError('unavailable', 503);
    });
    mount(w, ROUTES, '/notifications');
    const failure = await screen.findByTestId('notifications.error');
    w.client.on('GET /v1/notifications', () => ({ notifications: [], next_before: null }));
    fireEvent.click(within(failure).getByRole('button'));
    expect(await screen.findByTestId('notifications.empty')).toHaveTextContent('No notifications.');
    expect(screen.queryByTestId('notifications.mark-all')).toBeNull();
  });

  it('shows texts, marks read on click and navigates; pages; marks all', async () => {
    const w = await world();
    const rows = [
      notification('n3', {
        org_id: w.org,
        kind: 'task.idle',
        wg_id: w.wg,
        task_id: '0190f000-0000-7000-8000-00000000000c',
      }),
      notification('n2', { org_id: w.org, read_at: '2026-11-21T12:00:09.000Z' }),
      notification('n1', { org_id: w.org, created_at: '2026-11-21T12:00:01Z' }),
    ];
    w.client.on('GET /v1/notifications', (_body, target) =>
      target.includes('before=')
        ? { notifications: [notification('n0', { org_id: w.org, kind: 'rotation.required' })], next_before: null }
        : { notifications: rows, next_before: 'more' },
    );
    const { router } = mount(w, ROUTES, '/notifications');
    const items = () => screen.getAllByTestId('notifications.item');
    await waitFor(() => expect(items()).toHaveLength(3));
    expect(items().map((item) => item.dataset.unread)).toEqual(['true', 'false', 'true']);
    expect(within(items()[0]!).getByTestId('notifications.text')).toHaveTextContent('Task “…” is waiting for you');
    expect(within(items()[1]!).getByTestId('notifications.text')).toHaveTextContent(
      'New whiteboard entry in organization “…”',
    );
    expect(within(items()[2]!).queryByTestId('notifications.item.time')).toBeNull();
    expect(within(items()[0]!).getByTestId('notifications.item.time')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('notifications.more'));
    await waitFor(() => expect(items()).toHaveLength(4));
    expect(screen.queryByTestId('notifications.more')).toBeNull();
    // mark all: a failure, then success
    w.client.on('POST /v1/notifications/read', () => {
      throw new HubError('rate_limited', 429);
    });
    fireEvent.click(screen.getByTestId('notifications.mark-all'));
    expect(await screen.findByTestId('notifications.mark-all.error')).toBeInTheDocument();
    w.client.on('POST /v1/notifications/read', () => ({}));
    fireEvent.click(screen.getByTestId('notifications.mark-all'));
    await waitFor(() => expect(screen.queryByTestId('notifications.mark-all')).toBeNull());
    // a read row navigates without marking
    const calls = w.client.of('POST /v1/notifications/read').length;
    fireEvent.click(items()[0]!);
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    expect(w.client.of('POST /v1/notifications/read')).toHaveLength(calls);
  });

  it('windows many rows newest first and pages with the cursor (ADR-063 section 1)', async () => {
    const w = await world();
    const page = (newest: number, count: number) =>
      Array.from({ length: count }, (_, i) => {
        const n = newest - i;
        return notification(`n${String(n).padStart(4, '0')}`, {
          org_id: w.org,
          created_at: new Date(Date.UTC(2026, 10, 21, 12) + n * 1000).toISOString(),
        });
      });
    w.client.on('GET /v1/notifications', (_body, target) =>
      target.includes('before=')
        ? { notifications: page(100, 100), next_before: null }
        : { notifications: page(250, 150), next_before: 'more' },
    );
    mount(w, ROUTES, '/notifications');
    const items = () => screen.getAllByTestId('notifications.item');
    await waitFor(() => expect(items()).toHaveLength(WINDOW));
    const list = screen.getByTestId('notifications.list');
    expect(list.closest('[data-list]')).toHaveAttribute('data-list', 'notifications');
    // newest first, and only the window is rendered
    expect(within(list).getAllByRole('listitem')).toHaveLength(WINDOW);
    expect(items()[0]!.closest('li')).toHaveAttribute('data-key', 'n0250');
    expect(screen.queryByTestId('notifications.more')).toBeNull();
    fireEvent.click(screen.getByTestId('notifications.list.older'));
    expect(items()).toHaveLength(150);
    fireEvent.click(screen.getByTestId('notifications.more'));
    await waitFor(() => expect(items()).toHaveLength(150 + PAGE));
    fireEvent.click(screen.getByTestId('notifications.list.older'));
    expect(items()).toHaveLength(MAX_ROWS);
    expect(items()[0]!.closest('li')).toHaveAttribute('data-key', 'n0200');
    expect(screen.queryByTestId('notifications.more')).toBeNull();
  });

  it('navigates even when marking read fails', async () => {
    const w = await world();
    w.client.on('GET /v1/notifications', () => ({
      notifications: [notification('n1', { org_id: w.org, kind: 'invite.accepted' })],
      next_before: null,
    }));
    w.client.on('POST /v1/notifications/read', () => {
      throw new HubError('unavailable', 503);
    });
    const { router } = mount(w, ROUTES, '/notifications');
    fireEvent.click(await screen.findByTestId('notifications.item'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${w.org}/members`));
    expect(w.store.getState().notifications.rows.n1!.read_at).toBeNull();
  });
});

describe('bell', () => {
  it('shows the badge from the profile and live rows, capped at 99+', async () => {
    const w = await world();
    expect(headerItem).toMatchObject({ id: 'bell', order: 20 });
    setProfile(w.store, { unread_notifications: 0 });
    const { router } = mount(w, [{ path: '/', element: <Bell /> }], '/');
    expect(screen.queryByTestId('bell.badge')).toBeNull();
    expect(screen.getByTestId('bell.link')).toHaveAttribute('aria-label', 'Notifications');
    act(() => setProfile(w.store, { unread_notifications: 120 }));
    expect(screen.getByTestId('bell.badge')).toHaveTextContent('99+');
    act(() => setProfile(w.store, { unread_notifications: 2 }));
    act(() => receive(w.store, notification('n1', { org_id: w.org })));
    expect(screen.getByTestId('bell.badge')).toHaveTextContent('3');
    expect(screen.getByTestId('bell.link')).toHaveAttribute('aria-label', 'Notifications, 3 unread');
    fireEvent.click(screen.getByTestId('bell.link'));
    await waitFor(() => expect(router.state.location.pathname).toBe('/notifications'));
  });

  it('closes toasts by itself, with the button, or by opening the target', async () => {
    const w = await world();
    const { router } = mount(w, [{ path: '/', element: <Bell /> }], '/');
    vi.useFakeTimers();
    act(() => receive(w.store, notification('n1', { org_id: w.org })));
    expect(screen.getByTestId('bell.toast.open')).toHaveTextContent('New whiteboard entry in organization “…”');
    // hovering or focusing pauses the timer
    fireEvent.mouseEnter(screen.getByTestId('bell.toast'));
    expect(screen.getByTestId('bell.toast')).toHaveAttribute('data-paused', 'true');
    act(() => vi.advanceTimersByTime(TOAST_MS * 2));
    expect(screen.getByTestId('bell.toast')).toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByTestId('bell.toast'));
    fireEvent.focus(screen.getByTestId('bell.toast.close'));
    act(() => vi.advanceTimersByTime(TOAST_MS * 2));
    expect(screen.getByTestId('bell.toast')).toBeInTheDocument();
    fireEvent.blur(screen.getByTestId('bell.toast.close'));
    act(() => vi.advanceTimersByTime(TOAST_MS - 1));
    expect(screen.getByTestId('bell.toast')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByTestId('bell.toast')).toBeNull();
    act(() => receive(w.store, notification('n2', { org_id: w.org })));
    fireEvent.click(screen.getByTestId('bell.toast.close'));
    expect(screen.queryByTestId('bell.toast')).toBeNull();
    vi.useRealTimers();
    act(() => receive(w.store, notification('n3', { org_id: w.org, wg_id: w.wg })));
    fireEvent.click(screen.getByTestId('bell.toast.open'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${w.org}/w/${w.wg}/whiteboard`));
    await waitFor(() => expect(w.store.getState().notifications.rows.n3!.read_at).not.toBeNull());
  });
});
