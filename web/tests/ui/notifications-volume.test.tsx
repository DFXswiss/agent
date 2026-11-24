// U-WEB-ui-notifications (volume, KOR-35): the notification center stays shown through many window moves and loaded
// pages of a long list while rows arrive live. The hub orders by its exact time (microseconds), the client by the
// millisecond time it receives and the ID – rows of one millisecond may come in another order, so a loaded page can
// insert rows above the oldest shown one; live rows arrive at the newest end meanwhile.
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { receive } from '../../src/core/store/slices/notifications';
import { MAX_ROWS, NotificationsScreen } from '../../src/ui/screens/notifications/NotificationsScreen';
import { boardWorld } from '../core/whiteboards/world';
import { notification } from '../core/notifications/rows';
import { mount } from './chat-world';

const ROUTES = [{ path: '/notifications', element: <NotificationsScreen /> }];
const empty = { events: [], next_after: null, head: 0 };

/** Row `n` of the hub's order (newest first); three rows share each millisecond, their IDs run against that order. */
const row = (org: string, n: number) =>
  notification(`n-${String(Math.floor(n / 3)).padStart(5, '0')}-${2 - (n % 3)}`, {
    org_id: org,
    created_at: new Date(Date.UTC(2026, 10, 21, 12) - Math.floor(n / 3)).toISOString(),
  });

describe('a long notification list', () => {
  it('stays shown through many moves and loaded pages under live rows', { timeout: 120_000 }, async () => {
    const w = await boardWorld();
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
    w.client.on(`GET /v1/scopes/${w.wg}/events`, () => empty);
    // the hub's rows in its order: rows of one millisecond in reversed ID order (by the microseconds)
    const rows = Array.from({ length: 2000 }, (_, n) => row(w.org, n));
    w.client.on('GET /v1/notifications', (_body, target) => {
      const params = new URLSearchParams(target.split('?')[1]);
      const from = Number(params.get('before') ?? 0);
      const page = rows.slice(from, from + Number(params.get('limit')));
      return { notifications: page, next_before: from + page.length < rows.length ? String(from + page.length) : null };
    });
    mount(w, ROUTES, '/notifications');
    await screen.findByTestId('notifications.more');
    const moves: string[] = [];
    let live = 0;
    for (let step = 0; step < 60; step += 1) {
      const control = screen.queryByTestId('notifications.list.older') ?? screen.queryByTestId('notifications.more');
      if (control === null) break;
      moves.push(control.dataset.testid!);
      await act(async () => {
        fireEvent.click(control);
        // a live row arrives while the page loads
        live += 1;
        receive(
          w.store,
          notification(`z-live-${String(live).padStart(3, '0')}`, {
            org_id: w.org,
            created_at: new Date(Date.UTC(2026, 10, 21, 13) + live).toISOString(),
          }),
        );
        await Promise.resolve();
      });
      // the page arrived ("Load more" is enabled again or gone)
      await waitFor(() =>
        expect(screen.queryByTestId('notifications.more')?.hasAttribute('disabled') ?? false).toBe(false),
      );
      expect(screen.getByTestId('notifications.screen')).toBeInTheDocument();
      const shown = screen.getAllByTestId('notifications.item');
      expect(shown.length).toBeGreaterThan(0);
      expect(shown.length).toBeLessThanOrEqual(MAX_ROWS);
    }
    expect(moves.filter((name) => name === 'notifications.more').length).toBeGreaterThan(5);
  });
});
