// The membership confirmation in the header (KOR-36): it names the org, falls back to a neutral name while the org name
// is not known yet, shows only in the org that was joined, and closes. The browser suite reaches the fallback only when
// the join wins the race against loading the org directory, which depends on the machine; this covers it on every one.
import { act, fireEvent, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { noteJoined } from '../../src/core/store/slices/joined';
import { JoinedNotice } from '../../src/ui/shell/header/joined';
import { boardWorld } from '../core/whiteboards/world';
import { mount } from './chat-world';

describe('joined notice', () => {
  it('names an org without a known name neutrally, shows only in that org and closes', async () => {
    const w = await boardWorld();
    w.store.update('session', (session) => ({ ...session, activeOrg: w.org }));
    mount(w, [{ path: '/', element: <JoinedNotice /> }], '/');
    expect(screen.queryByTestId('shell.joined')).toBeNull();
    act(() => noteJoined(w.store, { org: 'another org', orgName: 'Other' }));
    expect(screen.queryByTestId('shell.joined')).toBeNull();
    act(() => noteJoined(w.store, { org: w.org, orgName: '' }));
    const notice = screen.getByTestId('shell.joined');
    expect(notice).toHaveAttribute('data-org', w.org);
    expect(notice.textContent).not.toContain('undefined');
    expect(notice.textContent).toMatch(/^You are now a member of /);
    fireEvent.click(screen.getByTestId('shell.joined.close'));
    expect(screen.queryByTestId('shell.joined')).toBeNull();
  });
});
