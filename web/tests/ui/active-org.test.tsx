// U-WEB-settings (KOR-68): the organization of the settings sections of the active org – its name, the choice in
// place (active and last used), the names read from the logs, and the line without any org.
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { putEvents } from '../../src/core/store/slices/events';
import { setProfile } from '../../src/core/store/slices/me';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { createI18n } from '../../src/core/i18n';
import { I18nProvider } from '../../src/ui/i18n';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { ActiveOrg } from '../../src/ui/settings/ActiveOrg';
import { taskWorld, type TaskWorld } from '../core/tasks/world';

function mountPicker(t: TaskWorld, locale: 'en' | 'de' = 'en') {
  const activate = vi.fn((org: string) => setActiveOrg(t.store, org));
  const rememberOrg = vi.fn();
  const app = { store: t.store, tasks: t.context, activate, rememberOrg } as never;
  const router = createMemoryRouter([{ path: '/', element: <ActiveOrg testId="x.org" /> }], { initialEntries: ['/'] });
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n(locale)}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return { activate, rememberOrg };
}

describe('active org of the settings', () => {
  it('says so without any org', async () => {
    const t = await taskWorld();
    setProfile(t.store, { orgs: [] });
    mountPicker(t);
    expect(screen.getByTestId('x.org.none')).toHaveTextContent('not a member of any organization');
    expect(screen.queryByTestId('x.org.select')).toBeNull();
  });

  it('names the orgs, reads missing logs and makes the chosen one active and the last used', async () => {
    const t = await taskWorld();
    const other = '0190f000-0000-7000-8000-0000000000ff';
    setProfile(t.store, {
      orgs: [
        { org_id: t.org, role: 'admin', private: false, default_wg: t.wg },
        { org_id: other, role: 'member', private: true, default_wg: t.wg },
      ],
    });
    // the org's verified profile names it
    putEvents(t.store, t.org, [
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { id: 'p1', kind: 'org.profile', author: t.me.id },
        body: { name: 'Acme' },
      },
    ] as never);
    const { activate, rememberOrg } = mountPicker(t, 'de');
    const select = screen.getByTestId('x.org.select');
    expect(select).toHaveValue('');
    expect(select).toHaveTextContent('Organisation wählen');
    expect(select).toHaveTextContent('(privat)');
    expect(select).toHaveTextContent('Acme');
    // the org without a verified log is read so its name can show
    expect(t.context.refreshLog).toHaveBeenCalledWith(other);
    act(() => {
      fireEvent.change(select, { target: { value: t.org } });
    });
    expect(activate).toHaveBeenCalledWith(t.org);
    expect(rememberOrg).toHaveBeenCalledWith(t.org);
    expect(select).toHaveValue(t.org);
    expect(select).not.toHaveTextContent('Organisation wählen');
  });

  it('survives a log that cannot be read', async () => {
    const t = await taskWorld();
    const other = '0190f000-0000-7000-8000-0000000000fe';
    setProfile(t.store, { orgs: [{ org_id: other, role: 'member', private: false, default_wg: t.wg }] });
    t.context.refreshLog.mockRejectedValueOnce(new Error('offline'));
    mountPicker(t);
    expect(screen.getByTestId('x.org.select')).toHaveTextContent('Organization');
    await vi.waitFor(() => expect(t.context.refreshLog).toHaveBeenCalledWith(other));
  });
});
