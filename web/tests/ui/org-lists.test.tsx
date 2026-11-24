// U-WEB-ui-org-lists (ADR-063 section 1, 06-web.md sections 7.1–7.4): the organization lists at volume – members,
// workgroups, devices, the open org and device invitations and the platform invitations render only their window
// (two pages at first, at most `MAX_ROWS`), the chips of a row are bounded with "N more – show", server paging shows
// "Load more", and the rows' memo compares content (`sameValue`).
import { fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { formatTime } from '../../src/core/encoding/time';
import { createI18n } from '../../src/core/i18n';
import type { DeviceRow } from '../../src/core/org/devices';
import type { InviteRow } from '../../src/core/org/invites';
import type { MemberRow } from '../../src/core/org/members';
import type { WorkgroupRow } from '../../src/core/org/workgroups';
import type { PlatformInviteRow } from '../../src/core/platform/invites';
import { DeviceList } from '../../src/ui/screens/devices/DevicesScreen';
import { CHIP_LIMIT } from '../../src/ui/screens/members/Chips';
import { MAX_ROWS, PAGE, sameValue } from '../../src/ui/screens/members/lists';
import { MemberList } from '../../src/ui/screens/members/MembersScreen';
import { InviteList } from '../../src/ui/screens/members/OpenInvites';
import { PlatformInviteRows } from '../../src/ui/screens/platform-invites/PlatformInvitesScreen';
import { WorkgroupList } from '../../src/ui/screens/workgroups/WorkgroupsScreen';
import { I18nProvider } from '../../src/ui/i18n';

const COUNT = 600;
const ids = (prefix: string, count = COUNT) =>
  Array.from({ length: count }, (_, i) => `${prefix}${String(i).padStart(4, '0')}`);
const wgs = ids('wg', 20);
const names = new Map(wgs.map((wg, i) => [wg, `Group ${i}`]));

function show(node: ReactNode) {
  return render(
    <I18nProvider value={createI18n('en')}>
      <MemoryRouter>{node}</MemoryRouter>
    </I18nProvider>,
  );
}

/** Rendered rows and the list's own counts. */
function shown(testId: string) {
  const list = screen.getByTestId(testId);
  return { rows: list.querySelectorAll(':scope > li').length, total: list.dataset.total, shown: list.dataset.shown };
}

/** Clicks "more" until the window reached the end of what is loaded. */
function showAll(testId: string) {
  for (let i = 0; i < 20 && screen.queryByTestId(`${testId}.older`) !== null; i += 1) {
    fireEvent.click(screen.getByTestId(`${testId}.older`));
  }
}

const member = (id: string, i: number): MemberRow => ({
  id,
  name: `Member ${i}`,
  role: i === 0 ? 'admin' : 'member',
  access: wgs.map((wg, n) => ({ wg, access: 'read', isDefault: n === 0, inherited: false })),
  isMe: i === 0,
  lastAdmin: i === 0,
  joinedSeq: i + 1,
});

describe('organization lists at volume', () => {
  // jsdom has no layout: "Jump to the newest" scrolls nowhere
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('renders at most the window of 600 members, with bounded chips', () => {
    const onOpen = vi.fn();
    show(<MemberList rows={ids('u').map(member)} admin names={names} onOpen={onOpen} />);
    expect(shown('members.list')).toEqual({ rows: 2 * PAGE, total: String(COUNT), shown: String(2 * PAGE) });
    expect(screen.getByTestId('members.list.older')).toHaveTextContent(`${COUNT - 2 * PAGE} more members – show`);
    showAll('members.list');
    expect(shown('members.list').rows).toBe(MAX_ROWS);
    expect(screen.getByTestId('members.list.newer')).toHaveTextContent('members above – show');
    fireEvent.click(screen.getByTestId('members.list.newest'));
    const first = screen.getAllByTestId('members.item')[0]!;
    expect(first).toHaveAttribute('data-member', 'u0000');
    // 20 workgroups: the first CHIP_LIMIT, the rest behind "12 more – show"
    const chips = within(first).getByTestId('members.item.access');
    expect(chips.querySelectorAll('[data-wg]')).toHaveLength(CHIP_LIMIT);
    fireEvent.click(within(first).getByTestId('members.item.access.more'));
    expect(chips.querySelectorAll('[data-wg]')).toHaveLength(wgs.length);
    expect(within(first).queryByTestId('members.item.access.more')).toBeNull();
    fireEvent.click(within(first).getAllByTestId('members.item.ungrant').at(-1)!);
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ kind: 'ungrant', wg: wgs.at(-1) }));
  });

  it('renders at most the window of 600 workgroups', () => {
    const rows = ids('w').map((id, i): WorkgroupRow => ({
      id,
      name: null,
      isDefault: i === 0,
      archived: false,
      myAccess: 'write',
      devices: [],
    }));
    show(<WorkgroupList org="o" rows={rows} admin onOpen={() => undefined} />);
    expect(shown('workgroups.list').rows).toBe(2 * PAGE);
    showAll('workgroups.list');
    expect(shown('workgroups.list').rows).toBe(MAX_ROWS);
    expect(screen.getByTestId('workgroups.list.newest')).toHaveTextContent('Back to the top');
  });

  it('renders at most the window of 600 devices, with bounded workgroup chips', () => {
    const rows = ids('d').map((id): DeviceRow => ({
      id,
      label: null,
      kind: 'org',
      platformSeq: null,
      decertified: false,
      workgroups: wgs,
      hub: null,
    }));
    show(<DeviceList rows={rows} admin={false} names={names} onOpen={() => undefined} />);
    expect(shown('devices.list').rows).toBe(2 * PAGE);
    expect(screen.getByTestId('devices.list.older')).toHaveTextContent('more devices – show');
    showAll('devices.list');
    expect(shown('devices.list').rows).toBe(MAX_ROWS);
    const first = screen.getAllByTestId('devices.item')[0]!;
    expect(within(first).getByTestId('devices.item.wgs').querySelectorAll('[data-wg]')).toHaveLength(CHIP_LIMIT);
    expect(within(first).getByTestId('devices.item.wgs.more')).toHaveTextContent('12 more – show');
  });

  it('windows 600 open invitations and asks the loader for the next page', () => {
    const rows = ids('i').map((id): InviteRow => ({
      invite_id: id,
      kind: 'device',
      state: 'open',
      created_at: formatTime(Date.now()),
      expires_at: formatTime(Date.now() + 3_600_000),
      created_by: 'u',
    }));
    const loadMore = vi.fn();
    const onRevoke = vi.fn();
    const paging = { hasMore: true, loading: false, loadMore };
    const view = show(
      <InviteList prefix="devices.invites" rows={rows} busy={false} onRevoke={onRevoke} paging={paging} />,
    );
    expect(shown('devices.invites.rows').rows).toBe(2 * PAGE);
    showAll('devices.invites.rows');
    expect(shown('devices.invites.rows').rows).toBe(MAX_ROWS);
    // the window has not reached the loaded end yet: no "Load more"
    expect(screen.queryByTestId('devices.invites.rows.load-more')).toBeNull();
    view.unmount();
    show(
      <InviteList prefix="members.invites" rows={rows.slice(0, 3)} busy={false} onRevoke={onRevoke} paging={paging} />,
    );
    fireEvent.click(screen.getByTestId('members.invites.rows.load-more'));
    expect(loadMore).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getAllByTestId('members.invites.revoke')[0]!);
    expect(onRevoke).toHaveBeenCalledWith('i0000');
  });

  it('windows 600 platform invitations and pages them', () => {
    const rows = ids('p').map((id): PlatformInviteRow => ({
      invite_id: id,
      kind: 'user',
      state: 'expired',
      created_by: null,
      created_at: formatTime(Date.now()),
      expires_at: formatTime(Date.now()),
      registered: false,
      used_at: null,
      revoked_at: null,
    }));
    const loadMore = vi.fn();
    const view = show(<PlatformInviteRows rows={rows} busy={false} onRevoke={() => undefined} />);
    expect(shown('platform-invites.rows').rows).toBe(2 * PAGE);
    showAll('platform-invites.rows');
    expect(shown('platform-invites.rows').rows).toBe(MAX_ROWS);
    view.unmount();
    show(
      <PlatformInviteRows
        rows={rows.slice(0, 2)}
        busy={false}
        onRevoke={() => undefined}
        paging={{ hasMore: true, loading: true, loadMore }}
      />,
    );
    expect(screen.getByTestId('platform-invites.rows.load-more')).toBeDisabled();
  });

  it('compares row content for the memo', () => {
    const map = new Map([['a', 'b']]);
    const fn = () => undefined;
    expect(
      sameValue({ row: { id: 'x', list: [1, 2] }, names: map, fn }, { row: { id: 'x', list: [1, 2] }, names: map, fn }),
    ).toBe(true);
    expect(sameValue({ list: [1, 2] }, { list: [1, 3] })).toBe(false);
    expect(sameValue({ a: 1 }, { a: 1, b: 2 })).toBe(false);
    expect(sameValue({ a: 1 }, { b: 1 })).toBe(false);
    expect(sameValue({ a: null }, { a: {} })).toBe(false);
    expect(sameValue({ a: {} }, { a: null })).toBe(false);
    expect(sameValue('x', 'y')).toBe(false);
    expect(sameValue({ fn }, { fn: () => undefined })).toBe(false);
    // maps are compared by identity only (one selector per list delivers the same map)
    expect(sameValue(map, new Map(map))).toBe(false);
    expect(sameValue({}, map)).toBe(false);
  });
});
