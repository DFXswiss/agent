// U-WEB-ui-members (screens): the member screen, the invitation area, the confirmation dialog ("Saving …", the attempt
// after `seq_conflict`) and the focus trap, in jsdom against the org world of tests/core/org/world.ts (verified log, keys, the
// simulated hub). The Playwright specs run the same screens against the real hub; these tests cover the states that
// are slow or impossible to produce there (refusals, retries, empty and unnamed rows, inherited access).
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { ActionDialog, advance, NO_PROGRESS, Progress } from '../../src/ui/screens/members/ActionDialog';
import { Dialog } from '../../src/ui/screens/members/Dialog';
import { expiryLabel } from '../../src/ui/screens/members/InviteSection';
import { MembersScreen } from '../../src/ui/screens/members/MembersScreen';
import { I18nProvider } from '../../src/ui/i18n';
import { idOf } from '../core/log/world';
import { orgWorld } from '../core/org/world';
import { mount, names } from './org-world';

afterEach(() => vi.useRealTimers());

async function membersWorld(as: 'ada' | 'bob' = 'ada') {
  const one = await idOf('wg one');
  const w = await orgWorld({
    as,
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.admitUser(a.ada, a.cy, 'admin', {});
    },
  });
  // the hub's pulls of profiles answer empty pages
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  return { w, one };
}

const at = (path: string) => [
  { path: '/o/:org/members', element: <MembersScreen /> },
  { path, element: <p /> },
];

describe('member screen', () => {
  it('shows loading, not found and the rows with names, roles and access', async () => {
    const { w, one } = await membersWorld();
    const { ada, bob, cy } = w.others;
    names(w, { [bob.id]: 'Bob', [ada.id]: 'Ada' }, { [w.world.defaultWg]: 'Standard' });
    mount(w, at('/x'), `/o/${w.org}/members`);
    const items = await screen.findAllByTestId('members.item');
    expect(items.map((item) => item.getAttribute('data-member'))).toEqual([ada.id, cy.id, bob.id]);
    expect(within(items[1]!).getByTestId('members.item.name')).toHaveTextContent(`Member …${cy.id.slice(-6)}`);
    expect(within(items[2]!).getByTestId('members.item.access')).toHaveTextContent('StandardWrite');
    expect(within(items[2]!).getByTestId('members.item.access')).toHaveTextContent('WorkgroupRead');
    expect(within(items[0]!).queryByTestId('members.item.remove')).toBeNull();
    // cy is an admin: workgroup one is read as admin, with nothing to ungrant (ADR-058)
    expect(within(items[1]!).getByTestId('members.item.access')).toHaveTextContent('Workgroupreads as admin');
    expect(within(items[1]!).queryByTestId('members.item.ungrant')).toBeNull();
    expect(screen.getByTestId('members.leave')).toBeInTheDocument();
    expect(one).toBeTruthy();
  });

  it('says loading before the log and not found for a non-member', async () => {
    const { w } = await membersWorld();
    mount(w, at('/x'), `/o/other/members`);
    expect(screen.getByTestId('members.loading')).toBeInTheDocument();
    const gone = await membersWorld('bob');
    await gone.w.world.batch(gone.w.others.bob, [{ type: 'remove', subject: gone.w.others.bob.id }]);
    await gone.w.sync();
    mount(gone.w, at('/x'), `/o/${gone.w.org}/members`);
    expect(screen.getAllByTestId('members.not-found')).toHaveLength(1);
  });

  it('changes a role, grants with history, ungrants and removes, each through the dialog', async () => {
    const { w, one } = await membersWorld();
    const { bob, cy } = w.others;
    mount(w, at('/x'), `/o/${w.org}/members`);
    const row = () => screen.getAllByTestId('members.item').find((item) => item.dataset.member === bob.id)!;
    // ungranting a member: it loses the workgroup (the dialog says so); cancelled here
    fireEvent.click(within(row()).getByTestId('members.item.ungrant'));
    expect(screen.getByTestId('members.ungrant')).toHaveTextContent('they cannot read new content');
    fireEvent.click(screen.getByTestId('members.ungrant.cancel'));
    // role
    fireEvent.click(within(row()).getByTestId('members.item.role-change'));
    fireEvent.click(screen.getByTestId('members.role.confirm'));
    await waitFor(() => expect(screen.queryByTestId('members.role')).toBeNull());
    expect(w.world.state.isAdmin(bob.id)).toBe(true);
    // cancel leaves things as they are
    fireEvent.click(within(row()).getByTestId('members.item.role-change'));
    fireEvent.click(screen.getByTestId('members.role.cancel'));
    expect(screen.queryByTestId('members.role')).toBeNull();
    // grant: cy has no access to workgroup one
    const cyRow = () => screen.getAllByTestId('members.item').find((item) => item.dataset.member === cy.id)!;
    fireEvent.click(within(cyRow()).getByTestId('members.item.grant'));
    fireEvent.change(screen.getByTestId('members.grant.wg'), { target: { value: one } });
    fireEvent.change(screen.getByTestId('members.grant.access'), { target: { value: 'write' } });
    fireEvent.click(screen.getByTestId('members.grant.history'));
    fireEvent.click(screen.getByTestId('members.grant.confirm'));
    await waitFor(() => expect(screen.queryByTestId('members.grant')).toBeNull());
    expect(w.world.state.wgAccess(cy.id, one)).toBe('write');
    // ungrant bob's explicit access to workgroup one: as an admin now, bob keeps reading it as admin
    fireEvent.click(within(row()).getByTestId('members.item.ungrant'));
    expect(screen.getByTestId('members.ungrant')).toHaveTextContent('Workgroup');
    expect(screen.getByTestId('members.ungrant')).toHaveTextContent('stays an admin and keeps reading');
    fireEvent.click(screen.getByTestId('members.ungrant.confirm'));
    await waitFor(() => expect(screen.queryByTestId('members.ungrant')).toBeNull());
    expect(w.world.state.explicitAccess(bob.id, one)).toBeNull();
    expect(w.world.state.inherited(bob.id, one)).toBe(true);
    // demoting an admin: one honest sentence, "Saving …", and the rotation is built automatically
    const epoch = w.world.state.epoch('wg', one);
    fireEvent.click(within(row()).getByTestId('members.item.role-change'));
    expect(screen.getByTestId('members.role.demote')).toHaveTextContent('Content the person has already read');
    fireEvent.click(screen.getByTestId('members.role.confirm'));
    expect(screen.getByTestId('members.role.progress.save')).toHaveTextContent('Saving …');
    await waitFor(() => expect(screen.queryByTestId('members.role')).toBeNull());
    expect(w.world.state.isAdmin(bob.id)).toBe(false);
    expect(w.world.state.epoch('wg', one)).toBe(epoch + 1);
    // remove
    fireEvent.click(within(row()).getByTestId('members.item.remove'));
    fireEvent.click(screen.getByTestId('members.remove.confirm'));
    await waitFor(() => expect(screen.queryByTestId('members.remove')).toBeNull());
    expect(w.world.state.member(bob.id)).toBeNull();
  });

  it('offers no grant when every workgroup is granted, shows refusals and leaves', async () => {
    const { w } = await membersWorld('bob');
    const { ada } = w.others;
    const { router, app } = mount(w, at('/'), `/o/${w.org}/members`);
    expect(screen.queryByTestId('members.invite.open')).toBeNull();
    expect(screen.queryByTestId('members.item.grant')).toBeNull();
    fireEvent.click(screen.getByTestId('members.leave'));
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('members.leave-dialog.confirm'));
    expect(await screen.findByTestId('members.leave-dialog.error')).toHaveAttribute('data-code', 'forbidden');
    w.client.on(`POST /v1/orgs/${w.org}/log`, (body) => w.hub.append(w.me.id, body as never));
    fireEvent.click(screen.getByTestId('members.leave-dialog.confirm'));
    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
    expect(app.loadMe).toHaveBeenCalled();
    expect(ada).toBeTruthy();
  });

  it('says so when there is no workgroup left to grant', async () => {
    const { w } = await membersWorld();
    const { bob } = w.others;
    // workgroup one is archived: nothing can be granted
    await w.world.batch(w.others.ada, [{ type: 'wg_archive', wg: await idOf('wg one') }]);
    await w.sync();
    mount(w, at('/x'), `/o/${w.org}/members`);
    const row = screen.getAllByTestId('members.item').find((item) => item.dataset.member === bob.id)!;
    fireEvent.click(within(row).getByTestId('members.item.grant'));
    expect(screen.getByTestId('members.grant.none')).toBeInTheDocument();
    expect(screen.getByTestId('members.grant.confirm')).toBeDisabled();
  });
});

describe('invitations', () => {
  it('creates an invitation with chosen workgroups, shows it once and copies link and code', async () => {
    const { w, one } = await membersWorld();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    mount(w, at('/x'), `/o/${w.org}/members`);
    expect(await screen.findByTestId('members.invites.empty')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('members.invite.open'));
    fireEvent.change(screen.getByTestId('members.invite.role'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByTestId(`members.invite.wg.${one}`), { target: { value: 'write' } });
    fireEvent.change(screen.getByTestId(`members.invite.wg.${one}`), { target: { value: '' } });
    fireEvent.change(screen.getByTestId(`members.invite.wg.${one}`), { target: { value: 'read' } });
    fireEvent.change(screen.getByTestId('members.invite.expiry'), { target: { value: '720' } });
    expect(screen.getByTestId('members.invite.expiry')).toHaveDisplayValue('30 days');
    fireEvent.click(screen.getByTestId('members.invite.submit'));
    const code = (await screen.findByTestId('members.invite.code')).textContent!;
    expect(screen.getByTestId('members.invite.link')).toHaveTextContent(`https://hub.example/invite#${code}`);
    const invite = [...w.world.state.invites.values()].at(-1)!;
    expect(invite).toMatchObject({ role: 'admin', wgs: { [one]: 'read' }, expires: '2026-12-21T12:00:00.000Z' });
    fireEvent.click(screen.getByTestId('members.invite.copy-link'));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('members.invite.copy-code'));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(code));
    writeText.mockRejectedValueOnce(new Error('denied'));
    fireEvent.click(screen.getByTestId('members.invite.copy-link'));
    await waitFor(() => expect(screen.queryByText('Copied')).toBeNull());
    fireEvent.click(screen.getByTestId('members.invite.done'));
    expect(screen.queryByTestId('members.invite.code')).toBeNull();
    expect(await screen.findAllByTestId('members.invites.item')).toHaveLength(1);
  });

  it('shows refusals of create, list and revoke and revokes', async () => {
    const { w } = await membersWorld();
    w.client.on(`GET /v1/orgs/${w.org}/invites`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(w, at('/x'), `/o/${w.org}/members`);
    expect(await screen.findByTestId('members.invites.error')).toHaveAttribute('data-code', 'unavailable');
    // create refused
    fireEvent.click(screen.getByTestId('members.invite.open'));
    const route = `POST /v1/orgs/${w.org}/invites`;
    const original = w.client.routes.get(route)!;
    w.client.on(route, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('members.invite.submit'));
    expect(await screen.findByTestId('members.invite.error')).toHaveAttribute('data-code', 'forbidden');
    fireEvent.click(screen.getByTestId('members.invite.cancel'));
    expect(screen.queryByTestId('members.invite.form')).toBeNull();
    // create and revoke
    w.client.on(route, original);
    w.client.routes.delete(`GET /v1/orgs/${w.org}/invites`);
    w.client.on(`GET /v1/orgs/${w.org}/invites`, () => ({ invites: [...w.hub.invites.values()] }));
    fireEvent.click(screen.getByTestId('members.invite.open'));
    fireEvent.click(screen.getByTestId('members.invite.submit'));
    fireEvent.click(await screen.findByTestId('members.invite.done'));
    expect(await screen.findAllByTestId('members.invites.item')).toHaveLength(1);
    const id = [...w.hub.invites.keys()][0]!;
    w.client.on(`POST /v1/orgs/${w.org}/invites/${id}/revoke`, () => {
      throw new HubError('rate_limited', 429);
    });
    fireEvent.click(screen.getByTestId('members.invites.revoke'));
    expect(await screen.findByTestId('members.invites.error')).toHaveAttribute('data-code', 'rate_limited');
    w.client.on(`POST /v1/orgs/${w.org}/invites/${id}/revoke`, async (body) => {
      const entry = (body as { entry: { body: { seq: number } } }).entry;
      const result = await w.hub.append(w.me.id, {
        expected_seq: entry.body.seq,
        entries: [entry as never],
        wraps: [],
        events: [],
      });
      w.hub.invites.get(id)!.state = 'revoked';
      return { seq: result.head.seq };
    });
    fireEvent.click(screen.getByTestId('members.invites.revoke'));
    expect(await screen.findByTestId('members.invites.empty')).toBeInTheDocument();
  });

  it('shows the inviter where each invitation stands (KOR-36)', async () => {
    const { w } = await membersWorld();
    const { bob } = w.others;
    // bob has a name, cy none: the accepted invitations name the member or "the invited person"
    names(w, { [bob.id]: 'Bob' });
    mount(w, at('/x'), `/o/${w.org}/members`);
    const accepted = await screen.findAllByTestId('members.invites.accepted');
    expect(accepted.map((item) => item.textContent)).toEqual([
      'Accepted – the invited person is a member',
      'Accepted – Bob is a member',
    ]);
    // only accepted ones: "no open invitations"
    expect(screen.getByTestId('members.invites.empty')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('members.invite.open'));
    fireEvent.click(screen.getByTestId('members.invite.submit'));
    fireEvent.click(await screen.findByTestId('members.invite.done'));
    const item = await screen.findByTestId('members.invites.item');
    expect(item).toHaveAttribute('data-status', 'open');
    expect(within(item).getByTestId('members.invites.status')).toHaveTextContent('Not opened yet');
    expect(screen.queryByTestId('members.invites.empty')).toBeNull();
  });

  it('labels expiries in hours and days', () => {
    expect(expiryLabel(6)).toEqual({ key: 'members.invite.expiry.hours', count: 6 });
    expect(expiryLabel(72)).toEqual({ key: 'members.invite.expiry.days', count: 3 });
  });
});

describe('dialog and progress', () => {
  it('traps the focus, closes on Escape unless busy and returns the focus', async () => {
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const close = vi.fn();
    const view = render(
      <Dialog title="T" testId="d" onClose={close}>
        <button type="button">first</button>
        <button type="button">last</button>
      </Dialog>,
    );
    const [first, last] = within(screen.getByTestId('d')).getAllByRole('button');
    expect(first).toHaveFocus();
    fireEvent.keyDown(first!, { key: 'Tab', shiftKey: true });
    expect(last).toHaveFocus();
    fireEvent.keyDown(last!, { key: 'Tab' });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first!, { key: 'Tab' });
    fireEvent.keyDown(first!, { key: 'a' });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first!, { key: 'Escape' });
    expect(close).toHaveBeenCalledTimes(1);
    view.rerender(
      <Dialog title="T" testId="d" busy onClose={close}>
        <p>nothing focusable</p>
      </Dialog>,
    );
    fireEvent.keyDown(screen.getByTestId('d'), { key: 'Escape' });
    fireEvent.keyDown(screen.getByTestId('d'), { key: 'Tab' });
    expect(close).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it('shows "Saving …" and the attempt after seq_conflict', () => {
    const { rerender } = render(
      <I18nProvider value={createI18n('en')}>
        <Progress view={NO_PROGRESS} testId="p" />
      </I18nProvider>,
    );
    expect(screen.getByTestId('p.progress')).toHaveTextContent('Saving …');
    expect(screen.queryByTestId('p.progress.attempt')).toBeNull();
    rerender(
      <I18nProvider value={createI18n('en')}>
        <Progress view={advance({ step: 'retry', attempt: 2 })} testId="p" />
      </I18nProvider>,
    );
    expect(screen.getByTestId('p.progress.attempt')).toHaveTextContent('attempt 2 of 3');
    expect(screen.getByTestId('p.progress.save')).toBeInTheDocument();
  });

  it('runs, reports progress, shows a refusal and retries', async () => {
    let release: () => void = () => undefined;
    const run = vi.fn(
      (progress: (step: never) => void) =>
        new Promise<void>((resolve, reject) => {
          progress({ step: 'retry', attempt: 2 } as never);
          if (run.mock.calls.length === 1) reject(new HubError('last_admin', 403));
          else release = resolve;
        }),
    );
    const done = vi.fn();
    render(
      <I18nProvider value={createI18n('en')}>
        <ActionDialog title="T" testId="a" confirmLabel="Go" run={run} onDone={done} onClose={() => undefined} />
      </I18nProvider>,
    );
    fireEvent.click(screen.getByTestId('a.confirm'));
    expect(await screen.findByTestId('a.error')).toHaveAttribute('data-code', 'last_admin');
    fireEvent.click(screen.getByTestId('a.confirm'));
    expect(await screen.findByTestId('a.progress.attempt')).toBeInTheDocument();
    expect(screen.getByTestId('a.confirm')).toBeDisabled();
    expect(screen.getByTestId('a.cancel')).toBeDisabled();
    await act(async () => release());
    expect(done).toHaveBeenCalled();
  });
});
