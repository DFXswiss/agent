// U-WEB-ui-platform-invites (screen): the route only for verified inviters (loading, not available), the header link
// only for inviters, creating an invitation shown once (the same ID again after a failure), the list with every state
// and the revocation – in jsdom against the org world of tests/core/org/world.ts.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { formatTime } from '../../src/core/encoding/time';
import { VerifiedPlatformLog } from '../../src/core/log/platform';
import { pairKey } from '../../src/core/log/state';
import { HubError } from '../../src/core/net/signedFetch';
import type { PlatformInviteRow } from '../../src/core/platform/invites';
import { setPlatform } from '../../src/core/store/slices/orgs';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { PlatformInvitesScreen } from '../../src/ui/screens/platform-invites/PlatformInvitesScreen';
import { PlatformInvitesLink } from '../../src/ui/shell/header/platform-invites';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { mount } from './org-world';

const at = [
  {
    path: '/platform/invites',
    element: (
      <>
        <PlatformInvitesLink />
        <PlatformInvitesScreen />
      </>
    ),
  },
];

/** Grants (or not) the role `inviter` to the world's user in the browser's verified platform log. */
function inviter(w: OrgWorld, granted: boolean) {
  const current = w.store.getState().orgs.platform!;
  const roles = granted ? new Map([[pairKey(w.me.id, 'inviter'), 1]]) : new Map<string, number>();
  setPlatform(w.store, new VerifiedPlatformLog(current.entries, [...current.states, current.state.with({ roles })]));
}

const row = (id: string, extra: Partial<PlatformInviteRow> = {}): PlatformInviteRow => ({
  invite_id: id,
  kind: 'user',
  state: 'open',
  created_by: null,
  created_at: formatTime(Date.now() - 60_000),
  expires_at: formatTime(Date.now() + 3600_000),
  registered: false,
  used_at: null,
  revoked_at: null,
  ...extra,
});

describe('platform invitations', () => {
  it('waits for the platform log and is not available without the inviter role', async () => {
    const none = await orgWorld({ noPlatform: true });
    none.context.refreshLog.mockRejectedValueOnce(new Error('offline'));
    mount(none, at, '/platform/invites');
    expect(screen.getByTestId('platform-invites.loading')).toBeVisible();
    // without an active org nothing can refresh the platform log
    expect(none.context.refreshLog).not.toHaveBeenCalled();
    act(() => setActiveOrg(none.store, none.org));
    await waitFor(() => expect(none.context.refreshLog).toHaveBeenCalledWith(none.org));
    expect(screen.queryByTestId('shell.platform-invites')).toBeNull();
    const w = await orgWorld();
    inviter(w, false);
    mount(w, at, '/platform/invites');
    expect(screen.getByTestId('platform-invites.forbidden')).toBeVisible();
    expect(screen.queryByTestId('platform-invites.screen')).toBeNull();
  });

  it('creates an invitation shown once, lists every state and revokes open ones', async () => {
    const w = await orgWorld();
    inviter(w, true);
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const rows: PlatformInviteRow[] = [
      row('used', { state: 'used', used_at: formatTime(Date.now()), registered: true }),
      row('revoked', { state: 'revoked', revoked_at: formatTime(Date.now()) }),
      row('expired', { state: 'expired' }),
      row('registered', { registered: true }),
    ];
    // a fresh array per answer, as JSON over the wire
    w.client.on('GET /v1/platform/invites', () => ({ invites: [...rows] }));
    let fail = true;
    w.client.on('POST /v1/platform/invites', (raw) => {
      if (fail) {
        fail = false;
        throw new HubError('unavailable', 503);
      }
      const body = raw as { invite_id: string; expires_at: string };
      rows.unshift(row(body.invite_id, { expires_at: body.expires_at }));
      return {};
    });
    w.client.on('POST /v1/platform/invites/registered/revoke', () => {
      rows[rows.length - 1] = row('registered', { state: 'revoked', revoked_at: formatTime(Date.now()) });
      return {};
    });
    mount(w, at, '/platform/invites');
    expect(screen.getByTestId('shell.platform-invites')).toHaveAttribute('href', '/platform/invites');
    await waitFor(() => expect(screen.getAllByTestId('platform-invites.item')).toHaveLength(4));
    const states = screen.getAllByTestId('platform-invites.item').map((node) => node.dataset.state);
    expect(states).toEqual(['used', 'revoked', 'expired', 'open']);
    const [used, revoked] = screen.getAllByTestId('platform-invites.item');
    expect(used).toHaveTextContent('Used');
    expect(used).not.toHaveTextContent('Passkey registered');
    expect(revoked).toHaveTextContent('Revoked');
    expect(screen.getAllByTestId('platform-invites.item.revoke')).toHaveLength(1);
    expect(screen.getAllByTestId('platform-invites.item').at(-1)).toHaveTextContent('Passkey registered');
    fireEvent.click(screen.getByTestId('platform-invites.open'));
    fireEvent.click(screen.getByTestId('platform-invites.cancel'));
    fireEvent.click(screen.getByTestId('platform-invites.open'));
    expect(screen.getByTestId('platform-invites.expiry')).toHaveValue('72');
    fireEvent.change(screen.getByTestId('platform-invites.expiry'), { target: { value: '168' } });
    fireEvent.click(screen.getByTestId('platform-invites.submit'));
    await waitFor(() =>
      expect(screen.getByTestId('platform-invites.error')).toHaveAttribute('data-code', 'unavailable'),
    );
    // the retry must be the identical request: the expiry is fixed after the first attempt
    expect(screen.getByTestId('platform-invites.expiry')).toBeDisabled();
    fireEvent.click(screen.getByTestId('platform-invites.submit'));
    const code = (await screen.findByTestId('platform-invites.code')).textContent!;
    const [first, second] = w.client.of('POST /v1/platform/invites').map((call) => call.body as Record<string, string>);
    expect(second).toEqual(first);
    expect(Date.parse(second!.expires_at!) - Date.now()).toBeGreaterThan(167 * 3600_000);
    expect(JSON.stringify(w.client.calls)).not.toContain(code);
    expect(screen.getByTestId('platform-invites.link')).toHaveTextContent(`https://hub.example/invite#${code}`);
    expect(screen.getByTestId('platform-invites.warning')).toBeVisible();
    fireEvent.click(screen.getByTestId('platform-invites.copy-link'));
    await waitFor(() => expect(screen.getByTestId('platform-invites.copy-link')).toHaveTextContent('Copied'));
    writeText.mockRejectedValueOnce(new Error('denied'));
    fireEvent.click(screen.getByTestId('platform-invites.copy-code'));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('platform-invites.copy-code')).toHaveTextContent('Copy code');
    await waitFor(() => expect(screen.getAllByTestId('platform-invites.item')).toHaveLength(5));
    fireEvent.click(screen.getByTestId('platform-invites.done'));
    expect(screen.queryByTestId('platform-invites.code')).toBeNull();
    // revoke the open invitation of the list ("registered" stays revocable until it is used)
    const registered = screen.getAllByTestId('platform-invites.item').at(-1)!;
    fireEvent.click(within(registered).getByTestId('platform-invites.item.revoke'));
    await waitFor(() =>
      expect(screen.getAllByTestId('platform-invites.item').at(-1)).toHaveAttribute('data-state', 'revoked'),
    );
  });

  it('shows failures of the list and of a revocation, and an empty list', async () => {
    const w = await orgWorld();
    inviter(w, true);
    w.client.on('GET /v1/platform/invites', () => null);
    mount(w, at, '/platform/invites');
    await waitFor(() =>
      expect(screen.getByTestId('platform-invites.list.error')).toHaveAttribute('data-code', 'unavailable'),
    );
    const other = await orgWorld();
    inviter(other, true);
    other.client.on('GET /v1/platform/invites', () => ({ invites: [row('a')] }));
    other.client.on('POST /v1/platform/invites/a/revoke', () => {
      throw new HubError('invite_consumed', 409);
    });
    mount(other, at, '/platform/invites');
    const revoke = await screen.findByTestId('platform-invites.item.revoke');
    fireEvent.click(revoke);
    await waitFor(() =>
      expect(screen.getAllByTestId('platform-invites.list.error').at(-1)).toHaveAttribute(
        'data-code',
        'invite_consumed',
      ),
    );
    const empty = await orgWorld();
    inviter(empty, true);
    empty.client.on('GET /v1/platform/invites', () => ({ invites: [] }));
    mount(empty, at, '/platform/invites');
    await waitFor(() => expect(screen.getByTestId('platform-invites.list.empty')).toBeVisible());
  });

  it('reads the hub list page by page, keeps the loaded range on a re-read and shows a cut answer', async () => {
    const w = await orgWorld();
    inviter(w, true);
    let older = 'b';
    // a fresh array per answer, as JSON over the wire
    w.client.on('GET /v1/platform/invites', (_body, target) =>
      String(target).includes('before=')
        ? { invites: [row(older, { state: 'used' })], next_before: null }
        : { invites: [row('a')], next_before: 'c1' },
    );
    w.client.on('POST /v1/platform/invites/a/revoke', () => ({}));
    mount(w, at, '/platform/invites');
    await waitFor(() => expect(screen.getAllByTestId('platform-invites.item')).toHaveLength(1));
    fireEvent.click(screen.getByTestId('platform-invites.rows.load-more'));
    await waitFor(() => expect(screen.getAllByTestId('platform-invites.item')).toHaveLength(2));
    expect(screen.queryByTestId('platform-invites.rows.load-more')).toBeNull();
    // a revocation reads both loaded pages again
    older = 'c';
    fireEvent.click(screen.getByTestId('platform-invites.item.revoke'));
    await waitFor(() =>
      expect(screen.getAllByTestId('platform-invites.item').map((node) => node.dataset.invite)).toEqual(['a', 'c']),
    );
    expect(w.client.of('GET /v1/platform/invites').map((call) => call.target)).toEqual([
      '/v1/platform/invites?limit=100',
      '/v1/platform/invites?before=c1&limit=100',
      '/v1/platform/invites?limit=100',
      '/v1/platform/invites?before=c1&limit=100',
    ]);
    expect(screen.queryByTestId('platform-invites.list.truncated')).toBeNull();
    // an old hub refuses the parameters: the whole list once more, cut at its cap
    const old = await orgWorld();
    inviter(old, true);
    old.client.on('GET /v1/platform/invites', (_body, target) => {
      if (String(target).includes('?')) throw new HubError('request_invalid', 400);
      return { invites: [row('x')], truncated: true };
    });
    mount(old, at, '/platform/invites');
    await waitFor(() => expect(screen.getByTestId('platform-invites.list.truncated')).toBeVisible());
    expect(screen.getByTestId('platform-invites.list.truncated')).toHaveTextContent('only the newest invitations');
  });
});
