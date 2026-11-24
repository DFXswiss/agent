// U-WEB-ui-members (screens), ADR-060: "Add an existing account" on the member screen – an account found by its code
// or picked from the admin's invitees, role and workgroups, the admission and its refusals – and the user's own account
// code in the settings, in jsdom against the admission world of tests/core/org/admit-world.ts.
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatAccountCode } from '../../src/core/crypto/identity';
import { HubError } from '../../src/core/net/signedFetch';
import { setIdentity } from '../../src/core/store/slices/me';
import { MembersScreen } from '../../src/ui/screens/members/MembersScreen';
import { AccountCode } from '../../src/ui/settings/sections/passkeys/AccountCode';
import { admitWorld } from '../core/org/admit-world';
import { mount, names } from './org-world';

afterEach(() => vi.unstubAllGlobals());

async function world(as: 'ada' | 'cy' = 'ada') {
  const made = await admitWorld({ as });
  made.w.client.on(`GET /v1/scopes/${made.w.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  return made;
}

const members = (path: string) => [
  { path: '/o/:org/members', element: <MembersScreen /> },
  { path, element: <p /> },
];

describe('add an existing account', () => {
  it('is offered to admins only', async () => {
    const { w } = await world('cy');
    mount(w, members('/x'), `/o/${w.org}/members`);
    await screen.findAllByTestId('members.item');
    expect(screen.queryByTestId('members.admit.open')).toBeNull();
  });

  it('picks an invitee, chooses role and workgroups and admits; opening again clears the message', async () => {
    const { w, one, two, old, answer, invitees } = await world();
    const { bob, cy, dev2 } = w.others;
    invitees.push(
      { user_id: bob.id, invite_id: 'i1', registered_at: 'r1', identity: answer(bob) },
      { user_id: cy.id, invite_id: 'i2', registered_at: 'r2', identity: answer(cy) },
      { user_id: dev2.id, invite_id: 'i3', registered_at: 'r3', identity: null },
    );
    names(w, {}, { [w.world.defaultWg]: 'Standard', [one]: 'One' });
    mount(w, members('/x'), `/o/${w.org}/members`);
    fireEvent.click(screen.getByTestId('members.admit.open'));
    expect(screen.getByTestId('members.admit.form')).toHaveTextContent('Add an existing account');
    expect(screen.getByTestId('members.admit.form')).toHaveTextContent('Standard');
    // only bob: cy is a member, dev2 has no proven identity
    const picks = await screen.findAllByTestId('members.admit.invitee');
    const code = formatAccountCode(answer(bob).account_code);
    expect(picks.map((pick) => pick.textContent)).toEqual([code]);
    expect(screen.getByTestId('members.admit.submit')).toBeDisabled();
    expect(screen.getByTestId('members.admit.find')).toBeDisabled();
    // the archived workgroup is not offered, the unnamed one is
    expect(screen.queryByTestId(`members.admit.wg.${old}`)).toBeNull();
    expect(screen.getByTestId(`members.admit.wg.${two}`).closest('label')).toHaveTextContent('Workgroup');
    fireEvent.click(picks[0]!);
    expect(screen.getByTestId('members.admit.found')).toHaveTextContent(`Account ${code} found and verified`);
    fireEvent.change(screen.getByTestId('members.admit.role'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByTestId(`members.admit.wg.${one}`), { target: { value: 'write' } });
    fireEvent.change(screen.getByTestId(`members.admit.wg.${two}`), { target: { value: 'read' } });
    fireEvent.change(screen.getByTestId(`members.admit.wg.${two}`), { target: { value: '' } });
    // the name is required (ADR-073): every member sees it at once
    expect(screen.getByTestId('members.admit.submit')).toBeDisabled();
    fireEvent.change(screen.getByTestId('members.admit.name'), { target: { value: '  Bob Example ' } });
    fireEvent.click(screen.getByTestId('members.admit.submit'));
    expect(await screen.findByTestId('members.admit.done')).toHaveTextContent(
      `Bob Example (account ${code}) is a member now.`,
    );
    expect(screen.getByTestId('members.admit.done')).not.toHaveTextContent('could not be saved');
    // the name went out as a `member.name` event about bob, sealed for the org (the hub sees no name)
    const sent = w.ingested
      .flatMap((batch) => batch.items)
      .map((item) => (item.event as { header: Record<string, unknown> }).header);
    expect(sent.filter((header) => header.kind === 'member.name').map((header) => header.ref)).toEqual([bob.id]);
    expect(JSON.stringify(w.ingested)).not.toContain('Bob Example');
    expect(screen.queryByTestId('members.admit.form')).toBeNull();
    expect(w.world.state.isAdmin(bob.id)).toBe(true);
    expect(w.world.state.wgAccess(bob.id, one)).toBe('write');
    expect(w.world.state.explicitAccess(bob.id, two)).toBeNull();
    expect(w.world.state.wgAccess(bob.id, two)).toBe('read'); // an admitted admin inherits read (ADR-058)
    fireEvent.click(screen.getByTestId('members.admit.open'));
    expect(screen.queryByTestId('members.admit.done')).toBeNull();
  });

  it('finds an account by its code and explains what it cannot find', async () => {
    const { w, answer, invitees } = await world();
    const { bob, cy } = w.others;
    invitees.push({ user_id: cy.id, invite_id: 'i1', registered_at: 'r1', identity: answer(cy) });
    mount(w, members('/x'), `/o/${w.org}/members`);
    fireEvent.click(screen.getByTestId('members.admit.open'));
    const type = (text: string) => {
      fireEvent.change(screen.getByTestId('members.admit.code'), { target: { value: text } });
      fireEvent.click(screen.getByTestId('members.admit.find'));
    };
    type('nope');
    expect(await screen.findByTestId('members.admit.error')).toHaveAttribute('data-code', 'account_code_invalid');
    expect(screen.getByTestId('members.admit.error')).toHaveTextContent('Code not valid');
    type(formatAccountCode(answer(cy).account_code));
    await waitFor(() =>
      expect(screen.getByTestId('members.admit.error')).toHaveAttribute('data-code', 'already_member'),
    );
    expect(screen.getByTestId('members.admit.error')).toHaveTextContent('This account is a member already.');
    expect(screen.queryByTestId('members.admit.found')).toBeNull();
    type('AAAA-AAAA-AAAA');
    await waitFor(() => expect(screen.getByTestId('members.admit.error')).toHaveAttribute('data-code', 'not_found'));
    // the invitee list holds members only: nothing to pick
    expect(screen.queryByTestId('members.admit.invitees')).toBeNull();
    const code = formatAccountCode(answer(bob).account_code);
    type(code.toLowerCase());
    expect(await screen.findByTestId('members.admit.found')).toHaveTextContent(code);
    expect(screen.queryByTestId('members.admit.error')).toBeNull();
    fireEvent.change(screen.getByTestId('members.admit.name'), { target: { value: 'Bob' } });
    fireEvent.click(screen.getByTestId('members.admit.submit'));
    expect(await screen.findByTestId('members.admit.done')).toBeInTheDocument();
    expect(w.world.state.member(bob.id)).toMatchObject({ role: 'member', consented: false });
  });

  it('admits even when the name cannot be saved and the settled invitations cannot be read (ADR-073)', async () => {
    const { w, answer } = await world();
    const { bob } = w.others;
    w.client.on('POST /v1/events', () => {
      throw new HubError('unavailable', 503);
    });
    w.client.on(`GET /v1/orgs/${w.org}/invites`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(w, members('/x'), `/o/${w.org}/members`);
    fireEvent.click(screen.getByTestId('members.admit.open'));
    fireEvent.change(screen.getByTestId('members.admit.code'), { target: { value: answer(bob).account_code } });
    fireEvent.click(screen.getByTestId('members.admit.find'));
    await screen.findByTestId('members.admit.found');
    fireEvent.change(screen.getByTestId('members.admit.name'), { target: { value: 'Bob' } });
    fireEvent.click(screen.getByTestId('members.admit.submit'));
    expect(await screen.findByTestId('members.admit.done')).toHaveTextContent(
      'The name could not be saved – add it at the entry with "Enter name".',
    );
    expect(w.world.state.member(bob.id)).not.toBeNull();
  });

  it('shows a refused admission and closes on cancel', async () => {
    const { w, answer } = await world();
    const { bob } = w.others;
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new HubError('forbidden', 403);
    });
    mount(w, members('/x'), `/o/${w.org}/members`);
    fireEvent.click(screen.getByTestId('members.admit.open'));
    fireEvent.change(screen.getByTestId('members.admit.code'), {
      target: { value: answer(bob).account_code },
    });
    fireEvent.click(screen.getByTestId('members.admit.find'));
    await screen.findByTestId('members.admit.found');
    fireEvent.change(screen.getByTestId('members.admit.name'), { target: { value: 'Bob' } });
    fireEvent.click(screen.getByTestId('members.admit.submit'));
    await waitFor(() => expect(screen.getByTestId('members.admit.error')).toHaveAttribute('data-code', 'forbidden'));
    expect(screen.getByTestId('members.admit.error')).toHaveTextContent('No access');
    fireEvent.click(screen.getByTestId('members.admit.cancel'));
    expect(screen.queryByTestId('members.admit.form')).toBeNull();
    expect(screen.queryByTestId('members.admit.done')).toBeNull();
    expect(w.world.state.member(bob.id)).toBeNull();
  });

  it('refuses without the verified platform log and still opens without invitees', async () => {
    const { w, answer } = await world();
    w.store.update('orgs', (orgs) => ({ ...orgs, platform: null }));
    mount(w, members('/x'), `/o/${w.org}/members`);
    fireEvent.click(screen.getByTestId('members.admit.open'));
    fireEvent.change(screen.getByTestId('members.admit.code'), {
      target: { value: answer(w.others.bob).account_code },
    });
    fireEvent.click(screen.getByTestId('members.admit.find'));
    expect(await screen.findByTestId('members.admit.error')).toHaveAttribute('data-code', 'identity_unproven');
    expect(screen.queryByTestId('members.admit.invitees')).toBeNull();
    expect(screen.getByTestId('members.admit.form')).toHaveTextContent('Workgroup');
  });
});

describe('own account code', () => {
  it('shows the code computed from the unlocked identity and copies it', async () => {
    const { w, answer } = await world();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    mount(w, [{ path: '/s', element: <AccountCode /> }], '/s');
    const code = formatAccountCode(answer(w.others.ada).account_code);
    expect(await screen.findByTestId('passkeys.account-code.value')).toHaveTextContent(code);
    expect(screen.getByTestId('passkeys.account-code')).toHaveTextContent('Your account code');
    fireEvent.click(screen.getByTestId('passkeys.account-code.copy'));
    await waitFor(() => expect(screen.getByTestId('passkeys.account-code.copy')).toHaveTextContent('Copied'));
    expect(writeText).toHaveBeenCalledWith(code);
    writeText.mockRejectedValueOnce(new Error('denied'));
    fireEvent.click(screen.getByTestId('passkeys.account-code.copy'));
    await waitFor(() => expect(screen.getByTestId('passkeys.account-code.copy')).toHaveTextContent('Copy code'));
  });

  it('is hidden when the code cannot be computed and without an unlocked identity', async () => {
    const { w } = await world();
    const { ada } = w.others;
    // a user ID the code cannot be computed for
    w.store.update('me', (me) => ({ ...me, userId: 'not-a-uuid' }));
    mount(w, [{ path: '/s', element: <AccountCode /> }], '/s');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(screen.queryByTestId('passkeys.account-code')).toBeNull();
    w.store.update('me', (me) => ({ ...me, userId: ada.id }));
    act(() => setIdentity(w.store, null));
    act(() => setIdentity(w.store, ada.identity));
    expect(await screen.findByTestId('passkeys.account-code')).toBeInTheDocument();
    act(() => setIdentity(w.store, null));
    await waitFor(() => expect(screen.queryByTestId('passkeys.account-code')).toBeNull());
  });
});
