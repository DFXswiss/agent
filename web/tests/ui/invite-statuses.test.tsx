// U-WEB-ui-members (ADR-086 sections 1 and 2, KOR-36): the inviter's invitation list at volume – the statuses of the
// verified log's invitations with the hints of the hub's list read page by page, rendered as a window (two pages at
// first, at most `MAX_ROWS`); rows below the oldest invitation the read pages name wait for "Load more".
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { formatTime } from '../../src/core/encoding/time';
import { createI18n } from '../../src/core/i18n';
import type { Invite, OrgState, Principal } from '../../src/core/log/state';
import type { InviteRow } from '../../src/core/org/invites';
import { I18nProvider } from '../../src/ui/i18n';
import { InviteSection } from '../../src/ui/screens/members/InviteSection';
import { MAX_ROWS, PAGE } from '../../src/ui/screens/members/lists';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { FakeClient } from '../core/tasks/world';
import { EMPTY_DIRECTORY } from '../../src/core/tasks/directory';

const ORG_ID = 'org-1';
const NOW = Date.UTC(2026, 10, 24, 12);
const COUNT = 600;
const id = (n: number) => `inv${String(n).padStart(4, '0')}`;

/** A log with `COUNT` open user invitations (seq = n + 10) and two accepted ones (seq 1 and 2). */
function logState(): OrgState {
  const invites = new Map<string, Invite>();
  const invite = (inviteId: string, seq: number, acceptedSeq: number | null): Invite => ({
    inviteId,
    pkInv: 'pk',
    kind: 'user',
    role: 'member',
    wgs: {},
    expires: formatTime(NOW + 3_600_000),
    seq,
    revokedSeq: null,
    acceptedSeq,
  });
  invites.set('acc-a', invite('acc-a', 1, 3));
  invites.set('acc-b', invite('acc-b', 2, 4));
  for (let n = 0; n < COUNT; n += 1) invites.set(id(n), invite(id(n), n + 10, null));
  const principals = new Map<string, Principal>([['bob', { id: 'bob', joinedSeq: 3 } as Principal]]);
  return { invites, principals } as unknown as OrgState;
}

const hint = (inviteId: string, extra: Partial<InviteRow> = {}): InviteRow => ({
  invite_id: inviteId,
  kind: 'user',
  state: 'open',
  created_at: formatTime(NOW),
  expires_at: formatTime(NOW + 3_600_000),
  created_by: 'ada',
  ...extra,
});

/** The hub's list newest first in pages of `size`, `next_before` the index of the next page. */
function paged(client: FakeClient, rows: InviteRow[], size: number, extra: object = {}) {
  client.on(`GET /v1/orgs/${ORG_ID}/invites`, (_body, target) => {
    const before = new URLSearchParams(String(target).split('?')[1]).get('before');
    const start = before === null ? 0 : Number(before);
    const end = start + size;
    return { invites: rows.slice(start, end), next_before: end < rows.length ? String(end) : null, ...extra };
  });
}

function show(client: FakeClient, state: OrgState = logState()) {
  const app = { store: {}, tasks: { client, now: () => NOW }, config: { hub_key: null } };
  return render(
    <AppEnvProvider value={{ app: app as never, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>
        <InviteSection
          org={ORG_ID}
          state={state}
          directory={{
            ...EMPTY_DIRECTORY,
            orgName: 'Org',
            members: new Map([['bob', 'Bob']]),
            sources: new Map([['bob', { source: 'own', by: null }]]),
          }}
        />
      </I18nProvider>
    </AppEnvProvider>,
  );
}

const rows = () => screen.getByTestId('members.invites.rows').querySelectorAll(':scope > li');

describe('the inviter’s invitation list at volume', () => {
  // jsdom has no layout
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('windows 600 invitations and shows older ones only after their hub page was read', async () => {
    const client = new FakeClient();
    // newest first; the accepted ones are the oldest rows of the hub's list
    const all = [...Array.from({ length: COUNT }, (_, n) => hint(id(COUNT - 1 - n))), hint('acc-b'), hint('acc-a')];
    all[0] = hint(id(COUNT - 1), { opened_at: '2026-11-24T11:00:00.000Z' });
    all[1] = hint(id(COUNT - 2), { registered: true });
    paged(client, all, 100);
    show(client);
    await waitFor(() => expect(rows()).toHaveLength(2 * PAGE));
    const list = screen.getByTestId('members.invites.rows');
    // the first hub page names 100 invitations: nothing older is shown yet
    expect(list).toHaveAttribute('data-total', '100');
    expect(rows()[0]).toHaveAttribute('data-status', 'opened');
    expect(rows()[1]).toHaveAttribute('data-status', 'registered');
    expect(rows()[2]).toHaveAttribute('data-testid', 'members.invites.item');
    expect(screen.queryByTestId('members.invites.empty')).toBeNull();
    fireEvent.click(screen.getByTestId('members.invites.rows.load-more'));
    await waitFor(() => expect(list).toHaveAttribute('data-total', '200'));
    for (let i = 0; i < 20 && screen.queryByTestId('members.invites.rows.older') !== null; i += 1)
      fireEvent.click(screen.getByTestId('members.invites.rows.older'));
    expect(rows().length).toBeLessThanOrEqual(MAX_ROWS);
    // read to the end: the accepted ones follow at their place, named by the member who joined
    for (let i = 0; i < 10 && screen.queryByTestId('members.invites.rows.load-more') !== null; i += 1) {
      const total = list.getAttribute('data-total');
      fireEvent.click(screen.getByTestId('members.invites.rows.load-more'));
      await waitFor(() => expect(list.getAttribute('data-total')).not.toBe(total));
      for (let j = 0; j < 20 && screen.queryByTestId('members.invites.rows.older') !== null; j += 1)
        fireEvent.click(screen.getByTestId('members.invites.rows.older'));
    }
    await waitFor(() => expect(list).toHaveAttribute('data-total', String(COUNT + 2)));
    const accepted = screen.getAllByTestId('members.invites.accepted');
    expect(accepted.map((row) => within(row).getByTestId('members.invites.status').textContent)).toEqual([
      'Accepted – the invited person is a member',
      'Accepted – Bob is a member',
    ]);
    expect(rows().length).toBeLessThanOrEqual(MAX_ROWS);
  });

  it('offers "Load more" while the read pages name no invitation of the log, and says when the hub cut its list', async () => {
    const client = new FakeClient();
    // the newest page names only invitations the log does not know (device invitations of another kind, forged)
    paged(client, [hint('forged'), hint(id(5)), hint('acc-a')], 1, { truncated: true });
    show(client);
    const more = await screen.findByTestId('members.invites.rows.load-more');
    expect(screen.getByTestId('members.invites.truncated')).toBeInTheDocument();
    expect(screen.queryByTestId('members.invites.empty')).toBeNull();
    fireEvent.click(more);
    await waitFor(() => expect(rows()).toHaveLength(1));
    expect(rows()[0]).toHaveAttribute('data-status', 'open');
  });

  it('says "no open invitations" when every listed one is accepted and keeps the rows after a failed revocation', async () => {
    const client = new FakeClient();
    paged(client, [hint('acc-b'), hint('acc-a')], 100);
    const view = show(client);
    expect(await screen.findByTestId('members.invites.empty')).toBeInTheDocument();
    expect(screen.getAllByTestId('members.invites.accepted')).toHaveLength(2);
    view.unmount();
    paged(client, [hint(id(1))], 100);
    show(client);
    const revoke = await screen.findByTestId('members.invites.revoke');
    // the revocation reads the store's log; the stub has none: refused, the row stays
    await act(async () => fireEvent.click(revoke));
    expect(await screen.findByTestId('members.invites.error')).toBeInTheDocument();
    expect(screen.getAllByTestId('members.invites.item')).toHaveLength(1);
  });
});
