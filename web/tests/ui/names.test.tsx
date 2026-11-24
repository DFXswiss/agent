// ADR-067 (screens): how people are named – the helpers of ui/names (own, provisional and unknown names, who gave a
// name, a thing without a name with its ID on request), the member list with "Enter name"/"Change name" and the line
// that says who gave a provisional name, the name dialog, and the invitations with their names and the settled state
// with "Close invitation", in jsdom against the org world of tests/core/org/world.ts.
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { createInvite } from '../../src/core/org/invites';
import { putEvents } from '../../src/core/store/slices/events';
import { type Directory, EMPTY_DIRECTORY, MEMBER_NAME } from '../../src/core/tasks/directory';
import { givenBy, knownPersonName, NoName, personName } from '../../src/ui/names/names';
import { MembersScreen } from '../../src/ui/screens/members/MembersScreen';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { mount, names } from './org-world';

const { t } = createI18n('en');

/** Puts verified `member.name` events (admin-given names) into the events slice, after the profiles of `names`. */
function given(w: OrgWorld, items: readonly { ref: string; by: string; name: string }[]) {
  const base = 1000 + (w.store.getState().events[w.org]?.entries.size ?? 0);
  putEvents(
    w.store,
    w.org,
    items.map((item, i) => ({
      org: w.org,
      scopeId: w.org,
      scopeSeq: base + i,
      receivedAt: null,
      event: {},
      status: 'verified' as const,
      code: null,
      header: {
        kind: MEMBER_NAME,
        ref: item.ref,
        author: item.by,
        wg: '',
        lh: w.world.log!.head.seq,
        id: `g${base + i}`,
      },
      body: { display_name: item.name },
    })),
  );
}

/** ada (admin, me), bob (member), cy (admin), dev1 and dev2 admitted as users. */
async function namesWorld(as: 'ada' | 'bob' = 'ada') {
  const w = await orgWorld({
    as,
    setup: async (world, a) => {
      await world.admitUser(a.ada, a.bob, 'member', {});
      await world.admitUser(a.ada, a.cy, 'admin', {});
      await world.admitUser(a.ada, a.dev1, 'member', {});
      await world.admitUser(a.ada, a.dev2, 'member', {});
    },
  });
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null }));
  return w;
}

/** The invitation each user joined with. */
const inviteOf = (w: OrgWorld, user: string) => {
  const joined = w.world.state.member(user)!.joinedSeq;
  return [...w.world.state.invites.values()].find((invite) => invite.acceptedSeq === joined)!.inviteId;
};

const screens = [
  { path: '/o/:org/members', element: <MembersScreen /> },
  { path: '/x', element: <p /> },
];

const item = (id: string) => screen.getAllByTestId('members.item').find((row) => row.dataset.member === id)!;

describe('names helpers', () => {
  const directory: Directory = {
    ...EMPTY_DIRECTORY,
    members: new Map([
      ['own', 'Olga'],
      ['admin', 'Adam'],
      ['invited', 'Ivo'],
      ['orphan', 'Otto'],
    ]),
    sources: new Map([
      ['own', { source: 'own', by: null }],
      ['admin', { source: 'admin', by: 'own' }],
      ['invited', { source: 'invitation', by: 'nameless' }],
    ]),
  };

  it('names a person by the own name, a provisional name or a sentence', () => {
    expect(personName(t, directory, 'own')).toBe('Olga');
    expect(personName(t, directory, 'admin')).toBe('Adam (provisional)');
    expect(personName(t, directory, 'orphan')).toBe('Otto (provisional)');
    expect(personName(t, directory, 'nobody')).toBe('Name not known yet');
    expect(knownPersonName(t, directory, 'own')).toBe('Olga');
    expect(knownPersonName(t, directory, 'nobody')).toBeUndefined();
    expect(knownPersonName(t, directory, null)).toBeUndefined();
  });

  it('says who gave a provisional name, "an admin" when that person has no name', () => {
    expect(givenBy(t, directory, 'own')).toBeNull();
    expect(givenBy(t, directory, 'nobody')).toBeNull();
    expect(givenBy(t, directory, 'admin')).toBe(
      "Provisional name, entered by Olga. It is replaced by the person's own name as soon as they open this organization.",
    );
    expect(givenBy(t, directory, 'invited')).toMatch(/^Name from the invitation by an admin\./);
    const unknownBy: Directory = { ...directory, sources: new Map([['admin', { source: 'admin', by: null }]]) };
    expect(givenBy(t, unknownBy, 'admin')).toMatch(/^Provisional name, entered by an admin\./);
  });

  it('shows a thing without a name by a sentence, the ID only on request', () => {
    render(
      <>
        <NoName t={t} text="Device without a name" id="dev-1" testId="plain" />
        <NoName t={t} text="Device without a name" id="dev-2" testId="listed" showId />
      </>,
    );
    expect(screen.getByTestId('plain')).toHaveTextContent(/^Device without a name$/);
    expect(screen.getByTestId('plain')).toHaveAttribute('title', 'ID: dev-1');
    expect(screen.getByTestId('plain')).toHaveAttribute('data-id', 'dev-1');
    expect(screen.getByTestId('listed')).toHaveTextContent('Device without a nameID: dev-2');
  });
});

describe('member list names', () => {
  async function named(as: 'ada' | 'bob' = 'ada') {
    const w = await namesWorld(as);
    const { ada, bob, cy, dev1 } = w.others;
    names(w, { [ada.id]: 'Ada', [bob.id]: 'Bob' });
    given(w, [
      { ref: cy.id, by: ada.id, name: 'Cyrus' },
      // an own name is never replaced by a given one
      { ref: bob.id, by: ada.id, name: 'Robert' },
      { ref: inviteOf(w, dev1.id), by: cy.id, name: 'Dina' },
    ]);
    return w;
  }

  it('shows own, provisional and unknown names with who gave them, and lets an admin enter or change them', async () => {
    const w = await named();
    const { ada, bob, cy, dev1, dev2 } = w.others;
    mount(w, screens, `/o/${w.org}/members`);
    await screen.findAllByTestId('members.item');
    const name = (id: string) => within(item(id)).getByTestId('members.item.name');
    // own names: as they are, no "given by", no button (and none for oneself)
    expect(name(bob.id)).toHaveTextContent(/^Bob$/);
    expect(name(bob.id)).toHaveAttribute('data-source', 'own');
    expect(within(item(bob.id)).queryByTestId('members.item.given')).toBeNull();
    expect(within(item(bob.id)).queryByTestId('members.item.give-name')).toBeNull();
    expect(within(item(ada.id)).queryByTestId('members.item.give-name')).toBeNull();
    // provisional names: who gave them
    expect(name(cy.id)).toHaveTextContent(/^Cyrus$/);
    expect(name(cy.id)).toHaveAttribute('data-source', 'admin');
    expect(name(cy.id)).toHaveAttribute('title', `ID: ${cy.id}`);
    expect(within(item(cy.id)).getByTestId('members.item.given')).toHaveTextContent(
      'Provisional name, entered by Ada.',
    );
    expect(name(dev1.id)).toHaveAttribute('data-source', 'invitation');
    expect(within(item(dev1.id)).getByTestId('members.item.given')).toHaveTextContent(
      'Name from the invitation by Cyrus.',
    );
    // no name: a sentence, what happens next, and that the admin can enter it
    expect(name(dev2.id)).toHaveTextContent('Name not known yet');
    expect(within(item(dev2.id)).getByTestId('members.item.unnamed')).toHaveTextContent('You can also enter it now.');
    expect(within(item(dev2.id)).queryByTestId('members.item.given')).toBeNull();

    // the dialogs about a member call it by its marked name
    fireEvent.click(within(item(cy.id)).getByTestId('members.item.role-change'));
    expect(screen.getByTestId('members.role')).toHaveTextContent('Change the role of Cyrus (provisional)');
    fireEvent.click(screen.getByTestId('members.role.cancel'));
    fireEvent.click(within(item(bob.id)).getByTestId('members.item.role-change'));
    expect(screen.getByTestId('members.role')).toHaveTextContent('Change the role of Bob');
    expect(screen.getByTestId('members.role')).not.toHaveTextContent('provisional');
    fireEvent.click(screen.getByTestId('members.role.cancel'));

    // change a provisional name: the dialog starts with it
    fireEvent.click(within(item(cy.id)).getByTestId('members.item.give-name'));
    expect(within(item(cy.id)).getByTestId('members.item.give-name')).toHaveTextContent('Change name');
    expect(screen.getByTestId('members.name')).toHaveTextContent('Name for this member');
    expect(screen.getByTestId('members.name.input')).toHaveValue('Cyrus');
    fireEvent.click(screen.getByTestId('members.name.cancel'));
    expect(screen.queryByTestId('members.name')).toBeNull();

    // enter a name: refused while not valid, then sent as a sealed member.name about the member
    const open = within(item(dev2.id)).getByTestId('members.item.give-name');
    expect(open).toHaveTextContent('Enter name');
    fireEvent.click(open);
    const input = screen.getByTestId('members.name.input');
    expect(input).toHaveValue('');
    expect(screen.getByTestId('members.name.confirm')).toBeDisabled();
    expect(screen.getByTestId('members.name')).toHaveTextContent('Every member sees this name');
    fireEvent.change(input, { target: { value: '   ' } });
    expect(screen.getByTestId('members.name')).toHaveTextContent('Please enter a name of 1 to 80 characters.');
    expect(screen.getByTestId('members.name.confirm')).toBeDisabled();
    fireEvent.change(input, { target: { value: ' Dee ' } });
    expect(screen.getByTestId('members.name')).not.toHaveTextContent('Please enter a name');
    fireEvent.click(screen.getByTestId('members.name.confirm'));
    await waitFor(() => expect(screen.queryByTestId('members.name')).toBeNull());
    const sent = w.hub.events.at(-1) as { header: Record<string, unknown> };
    expect(sent.header).toMatchObject({ kind: MEMBER_NAME, ref: dev2.id, author: ada.id });
    expect(JSON.stringify(sent)).not.toContain('Dee');
  });

  it('tells a member that a missing name appears by itself, without controls', async () => {
    const w = await named('bob');
    const { cy, dev2 } = w.others;
    mount(w, screens, `/o/${w.org}/members`);
    await screen.findAllByTestId('members.item');
    const hint = within(item(dev2.id)).getByTestId('members.item.unnamed');
    expect(hint).toHaveTextContent('The name appears by itself as soon as the person opens this organization.');
    expect(hint).not.toHaveTextContent('You can also enter it now.');
    expect(within(item(cy.id)).getByTestId('members.item.given')).toHaveTextContent('entered by Ada');
    expect(screen.queryByTestId('members.item.give-name')).toBeNull();
  });
});

describe('invitation names', () => {
  it('names an invitation; one saved without its name can be named in the list', async () => {
    const w = await namesWorld();
    mount(w, screens, `/o/${w.org}/members`);
    expect(await screen.findByTestId('members.invites.empty')).toBeInTheDocument();
    // the name is required
    fireEvent.click(screen.getByTestId('members.invite.open'));
    expect(screen.getByTestId('members.invite.submit')).toBeDisabled();
    fireEvent.change(screen.getByTestId('members.invite.name'), { target: { value: 'Dora' } });
    // the invitation stands even when its name cannot be saved
    const events = w.client.routes.get('POST /v1/events')!;
    w.client.on('POST /v1/events', () => {
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('members.invite.submit'));
    fireEvent.click(await screen.findByTestId('members.invite.done'));
    const row = await screen.findByTestId('members.invites.item');
    expect(within(row).getByTestId('members.invites.for')).toHaveTextContent('Invitation without a name');
    expect(within(row).getByTestId('members.invites.revoke')).toHaveTextContent('Revoke');
    expect(row).toHaveTextContent('Expires');
    // name it from the list
    w.client.on('POST /v1/events', events);
    fireEvent.click(within(row).getByTestId('members.invites.give-name'));
    expect(screen.getByTestId('members.name')).toHaveTextContent('Name for this invitation');
    fireEvent.change(screen.getByTestId('members.name.input'), { target: { value: 'Dora' } });
    fireEvent.click(screen.getByTestId('members.name.confirm'));
    await waitFor(() => expect(screen.queryByTestId('members.name')).toBeNull());
    const id = [...w.hub.invites.keys()][0]!;
    expect(w.hub.events.at(-1)).toMatchObject({ header: { kind: MEMBER_NAME, ref: id } });
    // once the reader has it: "Invitation for Dora", nothing more to name
    given(w, [{ ref: id, by: w.me.id, name: 'Dora' }]);
    await waitFor(() =>
      expect(within(screen.getByTestId('members.invites.item')).getByTestId('members.invites.for')).toHaveTextContent(
        'Invitation for Dora',
      ),
    );
    expect(screen.queryByTestId('members.invites.give-name')).toBeNull();
  });

  it('shows an invitation whose account was admitted directly as settled and closes it', async () => {
    const w = await namesWorld();
    const { bob, cy, dev2 } = w.others;
    const ids: string[] = [];
    for (const fill of [3, 4, 5])
      ids.push(
        (
          await createInvite(w.context, w.org, {
            request: { kind: 'user', role: 'member', wgs: {}, hours: 72 },
            directory: EMPTY_DIRECTORY,
            hubKey: { id: 'hub-test', public_key: 'A'.repeat(43) },
            origin: 'https://hub.example',
            code: () => new Uint8Array(16).fill(fill),
          })
        ).inviteId,
      );
    const [forBob, forCy, forDee] = ids as [string, string, string];
    const settle = (id: string, user: string) =>
      w.hub.invites.set(id, { ...w.hub.invites.get(id)!, registered: true, registered_user: user } as never);
    settle(forBob, bob.id);
    settle(forCy, cy.id);
    settle(forDee, dev2.id);
    names(w, { [bob.id]: 'Bob' });
    given(w, [{ ref: forDee, by: w.me.id, name: 'Dee' }]);
    w.client.on(`POST /v1/orgs/${w.org}/invites/${forBob}/revoke`, async (body) => {
      const entry = (body as { entry: { body: { seq: number } } }).entry;
      const result = await w.hub.append(w.me.id, {
        expected_seq: entry.body.seq,
        entries: [entry as never],
        wraps: [],
        events: [],
      });
      w.hub.invites.get(forBob)!.state = 'revoked';
      return { seq: result.head.seq };
    });
    mount(w, screens, `/o/${w.org}/members`);
    const rows = await screen.findAllByTestId('members.invites.item');
    const row = (id: string) => rows[ids.length - 1 - ids.indexOf(id)]!;
    for (const id of ids) expect(row(id)).toHaveAttribute('data-status', 'settled');
    const status = (id: string) => within(row(id)).getByTestId('members.invites.status');
    // the member's name, else the invitation's, else "the invited person"; no expiry and nothing to name
    expect(status(forBob)).toHaveTextContent('Bob is a member now – this invitation is no longer needed');
    expect(status(forDee)).toHaveTextContent(/^Dee is a member now/);
    expect(status(forCy)).toHaveTextContent(/^the invited person is a member now/);
    expect(within(row(forDee)).getByTestId('members.invites.for')).toHaveTextContent('Invitation for Dee');
    expect(row(forBob)).not.toHaveTextContent('Expires');
    expect(within(row(forBob)).queryByTestId('members.invites.give-name')).toBeNull();
    expect(within(row(forBob)).getByTestId('members.invites.revoke')).toHaveTextContent('Close invitation');
    fireEvent.click(within(row(forBob)).getByTestId('members.invites.revoke'));
    await waitFor(() => expect(screen.getAllByTestId('members.invites.item')).toHaveLength(2));
    expect(w.world.state.invites.get(forBob)!.revokedSeq).not.toBeNull();
  });
});
