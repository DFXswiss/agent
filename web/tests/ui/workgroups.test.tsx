// U-WEB-ui-workgroups and U-WEB-ui-orgs (screens): the workgroup screen (create with members and devices, rename,
// archive, rights), the org switcher (names, switching, "Create organization" with its refusal), in jsdom against the org world of tests/core/org/world.ts.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { setProfile } from '../../src/core/store/slices/me';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { OrgSwitcher } from '../../src/ui/shell/header/org-switcher';
import { WorkgroupsScreen } from '../../src/ui/screens/workgroups/WorkgroupsScreen';
import { idOf } from '../core/log/world';
import { orgWorld } from '../core/org/world';
import { mount, names } from './org-world';

afterEach(() => vi.useRealTimers());

async function wgWorld(options: { as?: 'ada' | 'bob'; private?: boolean } = {}) {
  const one = await idOf('wg one');
  const old = await idOf('wg old');
  const w = await orgWorld({
    ...options,
    platformDevices: ['pdev'],
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_create', wg: old, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_archive', wg: old }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
      await world.admitDevice(a.ada, a.dev1, [one]);
      if (options.private) {
        await world.batch(a.ada, [{ type: 'device_assign', device: a.pdev.id, wg: world.defaultWg, platform_seq: 2 }]);
      }
    },
  });
  w.hub.platformAccess = true;
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  return { w, one, old };
}

const at = [{ path: '/o/:org/workgroups', element: <WorkgroupsScreen /> }];

describe('workgroup screen', () => {
  it('lists workgroups with access, devices and flags; readers get no controls', async () => {
    const { w, one, old } = await wgWorld({ as: 'bob' });
    names(w, {}, { [one]: 'One' });
    mount(w, at, `/o/${w.org}/workgroups`);
    const items = screen.getAllByTestId('workgroups.item');
    expect(items.map((item) => item.dataset.wg)).toEqual([w.world.defaultWg, one, old]);
    expect(within(items[1]!).getByTestId('workgroups.item.name')).toHaveTextContent('One');
    expect(within(items[1]!).getByTestId('workgroups.item.access')).toHaveTextContent('Read');
    expect(within(items[1]!).getByTestId('workgroups.item.devices')).toHaveTextContent('Devices: 1');
    expect(within(items[2]!).getByTestId('workgroups.item.archived')).toBeInTheDocument();
    expect(within(items[2]!).getByTestId('workgroups.item.name').tagName).toBe('SPAN');
    expect(screen.queryByTestId('workgroups.create.open')).toBeNull();
    expect(screen.queryByTestId('workgroups.item.rename')).toBeNull();
    // bob writes only the default workgroup, so only there he sets the default device
    expect(screen.getAllByTestId('workgroups.placement.open').map((node) => node.dataset.wg)).toEqual([
      w.world.defaultWg,
    ]);
  });

  it('sets the default device of a workgroup and shows it in the row (ADR-075)', async () => {
    const { w, one } = await wgWorld();
    const { dev1 } = w.others;
    let stored: unknown = { device_id: 'gone', vm_id: null };
    let release: () => void = () => undefined;
    const devices = [
      { device_id: dev1.id, online: true, vms: [{ vm_id: 'vm-1a2b3c4d', state: 'running', sessions: 1 }] },
    ];
    w.client.on(`GET /v1/workgroups/${w.world.defaultWg}`, () => ({ devices: [], placement: null }));
    w.client.on(`GET /v1/workgroups/${one}`, async () => {
      await new Promise<void>((resolve) => (release = resolve));
      return { devices, placement: stored };
    });
    let puts = 0;
    w.client.on(`PUT /v1/workgroups/${one}/placement`, (body) => {
      puts += 1;
      if (puts === 1) throw new HubError('placement_forbidden', 403);
      stored = body;
      return { placement: body };
    });
    names(w, {}, { [one]: 'One' });
    mount(w, at, `/o/${w.org}/workgroups`);
    const row = () => screen.getAllByTestId('workgroups.item').find((item) => item.dataset.wg === one)!;
    const opener = within(row()).getByTestId('workgroups.placement.open');
    expect(opener).toHaveAttribute('data-wg', one);
    act(() => release());
    // a default whose device is no longer listed counts as automatic
    await waitFor(() =>
      expect(within(row()).getByTestId('workgroups.item.placement')).toHaveTextContent('Default device: Automatic'),
    );
    fireEvent.click(opener);
    expect(screen.getByTestId('workgroups.placement.loading')).toBeVisible();
    expect(screen.getByTestId('workgroups.placement.save')).toBeDisabled();
    act(() => release());
    await waitFor(() => expect(screen.getByTestId('workgroups.placement.device')).toHaveValue('auto'));
    fireEvent.change(screen.getByTestId('workgroups.placement.device'), { target: { value: dev1.id } });
    fireEvent.change(screen.getByTestId('workgroups.placement.vm'), { target: { value: 'vm-1a2b3c4d' } });
    fireEvent.click(screen.getByTestId('workgroups.placement.save'));
    await waitFor(() =>
      expect(screen.getByTestId('workgroups.placement.error')).toHaveAttribute('data-code', 'placement_forbidden'),
    );
    fireEvent.click(screen.getByTestId('workgroups.placement.save'));
    await waitFor(() => expect(screen.queryByTestId('workgroups.placement')).toBeNull());
    expect(stored).toEqual({ device_id: dev1.id, vm_id: 'vm-1a2b3c4d' });
    act(() => release());
    await waitFor(() =>
      expect(within(row()).getByTestId('workgroups.item.placement')).toHaveTextContent(
        'Default device: Device without a name · vm-1a2b3c4d',
      ),
    );
    expect(within(row()).getByTestId('workgroups.item.placement')).toHaveAttribute('data-device', dev1.id);
    fireEvent.click(within(row()).getByTestId('workgroups.placement.open'));
    act(() => release());
    await waitFor(() => expect(screen.getByTestId('workgroups.placement.device')).toHaveValue(dev1.id));
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByTestId('workgroups.placement')).toBeNull();
    // the device without a VM
    fireEvent.click(within(row()).getByTestId('workgroups.placement.open'));
    act(() => release());
    await waitFor(() => expect(screen.getByTestId('workgroups.placement.vm')).toHaveValue('vm-1a2b3c4d'));
    fireEvent.change(screen.getByTestId('workgroups.placement.vm'), { target: { value: 'any' } });
    fireEvent.click(screen.getByTestId('workgroups.placement.save'));
    await waitFor(() => expect(screen.queryByTestId('workgroups.placement')).toBeNull());
    act(() => release());
    await waitFor(() =>
      expect(within(row()).getByTestId('workgroups.item.placement')).toHaveTextContent(
        'Default device: Device without a name',
      ),
    );
    expect(within(row()).getByTestId('workgroups.item.placement')).not.toHaveTextContent('vm-');
  });

  it('says loading before the log and not found for a non-member', async () => {
    const { w } = await wgWorld();
    mount(w, at, `/o/other/workgroups`);
    expect(screen.getByTestId('workgroups.loading')).toBeInTheDocument();
    const gone = await wgWorld({ as: 'bob' });
    await gone.w.world.batch(gone.w.others.bob, [{ type: 'remove', subject: gone.w.others.bob.id }]);
    await gone.w.sync();
    mount(gone.w, at, `/o/${gone.w.org}/workgroups`);
    expect(screen.getByTestId('workgroups.not-found')).toBeInTheDocument();
  });

  it('creates a workgroup with members and devices, renames and archives it', async () => {
    const { w } = await wgWorld({ private: true });
    const { bob, dev1, pdev } = w.others;
    mount(w, at, `/o/${w.org}/workgroups`);
    fireEvent.click(screen.getByTestId('workgroups.create.open'));
    expect(screen.getByTestId('workgroups.create.confirm')).toBeDisabled();
    fireEvent.change(screen.getByTestId('workgroups.create.name'), { target: { value: 'Research' } });
    const member = screen.getByTestId(`workgroups.create.member.${bob.id}`);
    fireEvent.change(member, { target: { value: 'write' } });
    fireEvent.change(member, { target: { value: '' } });
    fireEvent.change(member, { target: { value: 'read' } });
    expect(screen.getByTestId(`workgroups.create.device.${pdev.id}`).parentElement).toHaveTextContent(
      'Platform device',
    );
    expect(screen.getByTestId(`workgroups.create.device.${dev1.id}`).parentElement).toHaveTextContent(/^Device/);
    fireEvent.click(screen.getByTestId(`workgroups.create.device.${dev1.id}`));
    fireEvent.click(screen.getByTestId(`workgroups.create.device.${pdev.id}`));
    fireEvent.click(screen.getByTestId(`workgroups.create.device.${pdev.id}`));
    fireEvent.click(screen.getByTestId('workgroups.create.confirm'));
    await waitFor(() => expect(screen.queryByTestId('workgroups.create')).toBeNull());
    const state = w.world.state;
    const created = [...state.workgroups.keys()].at(-1)!;
    expect(state.wgAccess(bob.id, created)).toBe('read');
    expect(state.assigned(dev1.id, created)).toBe(true);
    expect(state.assigned(pdev.id, created)).toBe(false);
    // the new row (no name read yet) – rename
    const row = () => screen.getAllByTestId('workgroups.item').find((item) => item.dataset.wg === created)!;
    expect(within(row()).getByTestId('workgroups.item.name')).toHaveTextContent('Workgroup');
    fireEvent.click(within(row()).getByTestId('workgroups.item.rename'));
    expect(screen.getByTestId('workgroups.rename.name')).toHaveValue('');
    fireEvent.change(screen.getByTestId('workgroups.rename.name'), { target: { value: 'Renamed' } });
    fireEvent.click(screen.getByTestId('workgroups.rename.confirm'));
    await waitFor(() => expect(screen.queryByTestId('workgroups.rename')).toBeNull());
    expect(w.ingested.at(-1)!.items).toHaveLength(1);
    // archive
    fireEvent.click(within(row()).getByTestId('workgroups.item.archive'));
    fireEvent.click(screen.getByTestId('workgroups.archive.confirm'));
    await waitFor(() => expect(within(row()).getByTestId('workgroups.item.archived')).toBeInTheDocument());
    expect(w.world.state.writable(created)).toBe(false);
  });

  it('offers no member or device lists when there are none, and a rename keeps the known name', async () => {
    const w = await orgWorld({ noPlatform: true });
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
    names(w, {}, { [w.world.defaultWg]: 'Standard' });
    mount(w, at, `/o/${w.org}/workgroups`);
    expect(screen.queryByTestId('workgroups.item.archive')).toBeNull();
    fireEvent.click(screen.getByTestId('workgroups.item.rename'));
    expect(screen.getByTestId('workgroups.rename.name')).toHaveValue('Standard');
    fireEvent.click(screen.getByTestId('workgroups.rename.cancel'));
    fireEvent.click(screen.getByTestId('workgroups.create.open'));
    expect(screen.queryByText('Members')).toBeNull();
    expect(screen.queryByText('Devices')).toBeNull();
  });
});

async function switcherWorld() {
  const w = await orgWorld();
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  setProfile(w.store, {
    user_id: w.me.id,
    orgs: [
      { org_id: w.org, role: 'admin', private: false, default_wg: w.world.defaultWg },
      { org_id: 'unverified-org', role: 'member', private: true, default_wg: 'x' },
    ],
  });
  return w;
}

describe('org switcher', () => {
  it('lists the orgs with their names and switches', async () => {
    const w = await switcherWorld();
    const { router } = mount(w, [{ path: '*', element: <OrgSwitcher /> }], '/');
    expect(screen.getByTestId('org-switcher.current')).toHaveTextContent('Organizations');
    act(() => setActiveOrg(w.store, w.org));
    expect(screen.getByTestId('org-switcher.current')).toHaveTextContent('Organization');
    act(() => names(w, {}, {}));
    const items = screen.getAllByTestId('org-switcher.item');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveAttribute('aria-current', 'true');
    expect(items[1]).toHaveTextContent('private');
    fireEvent.click(items[1]!);
    expect(router.state.location.pathname).toBe('/o/unverified-org');
  });

  it('creates an organization and opens it; a refusal is shown', async () => {
    const w = await switcherWorld();
    act(() => setActiveOrg(w.store, w.org));
    names(w, { [w.me.id]: 'Ada' });
    const installed: string[] = [];
    const { router } = mount(w, [{ path: '*', element: <OrgSwitcher /> }], '/', {
      installOrg: vi.fn(async (prepared: { org: string }) => {
        installed.push(prepared.org);
      }),
    });
    fireEvent.click(screen.getByTestId('org-switcher.create.open'));
    expect(screen.getByTestId('org-switcher.create.display-name')).toHaveValue('Ada');
    fireEvent.change(screen.getByTestId('org-switcher.create.display-name'), { target: { value: 'Ada L.' } });
    expect(screen.getByTestId('org-switcher.create.platform-note')).toBeInTheDocument();
    expect(screen.getByTestId('org-switcher.create.confirm')).toBeDisabled();
    fireEvent.change(screen.getByTestId('org-switcher.create.name'), { target: { value: 'Acme' } });
    const posted: string[] = [];
    w.client.on('POST /v1/orgs', (body) => {
      posted.push((body as { genesis: { body: { org: string } } }).genesis.body.org);
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('org-switcher.create.confirm'));
    expect(await screen.findByTestId('org-switcher.create.error')).toHaveAttribute('data-code', 'unavailable');
    w.client.on('POST /v1/orgs', (body) => {
      const genesis = (body as { genesis: { body: { org: string; default_wg: string; private: boolean } } }).genesis
        .body;
      expect(genesis.private).toBe(false);
      // the retry sends the same prepared organization
      expect(genesis.org).toBe(posted[0]);
      return { org_id: genesis.org, default_wg: genesis.default_wg };
    });
    fireEvent.click(screen.getByTestId('org-switcher.create.confirm'));
    await waitFor(() => expect(installed).toHaveLength(1));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${installed[0]}`));
    expect(screen.queryByTestId('org-switcher.create')).toBeNull();
    fireEvent.click(screen.getByTestId('org-switcher.create.open'));
    fireEvent.click(screen.getByTestId('org-switcher.create.cancel'));
    expect(screen.queryByTestId('org-switcher.create')).toBeNull();
  });

  it('takes the own name as display name once it is known, unless the user typed one', async () => {
    // opened right after sign-in, before the profiles of the active org are read: the dialog must not keep the empty
    // name (its confirm button would stay disabled although the user filled in the organization name)
    const w = await switcherWorld();
    act(() => setActiveOrg(w.store, w.org));
    mount(w, [{ path: '*', element: <OrgSwitcher /> }], '/');
    fireEvent.click(screen.getByTestId('org-switcher.create.open'));
    fireEvent.change(screen.getByTestId('org-switcher.create.name'), { target: { value: 'Acme' } });
    expect(screen.getByTestId('org-switcher.create.display-name')).toHaveValue('');
    expect(screen.getByTestId('org-switcher.create.confirm')).toBeDisabled();
    act(() => names(w, { [w.me.id]: 'Ada' }));
    expect(screen.getByTestId('org-switcher.create.display-name')).toHaveValue('Ada');
    expect(screen.getByTestId('org-switcher.create.confirm')).toBeEnabled();
    fireEvent.change(screen.getByTestId('org-switcher.create.display-name'), { target: { value: '' } });
    expect(screen.getByTestId('org-switcher.create.display-name')).toHaveValue('');
    expect(screen.getByTestId('org-switcher.create.confirm')).toBeDisabled();
    fireEvent.change(screen.getByTestId('org-switcher.create.display-name'), { target: { value: 'Ada L.' } });
    act(() => names(w, { [w.me.id]: 'Ada Lovelace' }));
    expect(screen.getByTestId('org-switcher.create.display-name')).toHaveValue('Ada L.');
  });

  it('keeps the display name of a failed attempt for the retry', async () => {
    const w = await switcherWorld();
    act(() => setActiveOrg(w.store, w.org));
    names(w, { [w.me.id]: 'Ada' });
    mount(w, [{ path: '*', element: <OrgSwitcher /> }], '/');
    fireEvent.click(screen.getByTestId('org-switcher.create.open'));
    fireEvent.change(screen.getByTestId('org-switcher.create.name'), { target: { value: 'Acme' } });
    const posted: string[] = [];
    w.client.on('POST /v1/orgs', (body) => {
      posted.push((body as { genesis: { body: { org: string } } }).genesis.body.org);
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('org-switcher.create.confirm'));
    expect(await screen.findByTestId('org-switcher.create.error')).toHaveAttribute('data-code', 'unavailable');
    // the own name changes elsewhere: the retry still sends the prepared org of the first attempt
    act(() => names(w, { [w.me.id]: 'Ada Lovelace' }));
    expect(screen.getByTestId('org-switcher.create.display-name')).toHaveValue('Ada');
    fireEvent.click(screen.getByTestId('org-switcher.create.confirm'));
    await waitFor(() => expect(posted).toHaveLength(2));
    expect(posted[1]).toBe(posted[0]);
  });
});
