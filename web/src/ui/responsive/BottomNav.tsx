// Bottom navigation below `md` (06-web.md section 12.1): Home, Tasks, Notifications, Monitoring and More, reachable
// with the thumb. Tasks opens the task overview of the active org (all readable workgroups, KOR-32).
// More opens a sheet with every other navigation entry (discovered, ADR-027 section 4), the settings and the security
// page. Shown only with an active org (signed in).
import { useState } from 'react';
import { createPortal } from 'react-dom';
import { NavLink } from 'react-router-dom';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { type NavEntry, navEntries } from '../nav';
import { useAppEnv } from '../shell/context';
import { navTarget } from '../shell/Nav';
import { Sheet } from './Sheet';

/** Entries of the bar itself; the rest of the discovered entries go to "More". */
export const BAR_IDS = ['home', 'tasks', 'monitoring'];

const ITEM =
  'flex min-h-14 min-w-11 flex-col items-center justify-center px-1 text-xs aria-[current=page]:font-semibold ' +
  'aria-[current=page]:text-blue-800';

function More({ entries, org, onClose }: { entries: readonly NavEntry[]; org: string; onClose: () => void }) {
  const { t } = useI18n();
  const links = [
    ...entries
      .filter((entry) => !BAR_IDS.includes(entry.id))
      .map((entry) => ({ id: entry.id, to: navTarget(entry, org)!, label: t(entry.labelKey) })),
    { id: 'settings', to: '/settings', label: t('shell.settings') },
    { id: 'security', to: '/security', label: t('security.nav') },
  ];
  return (
    <Sheet title={t('shell.more.title')} testId="shell.more" onClose={onClose}>
      <ul className="flex flex-col">
        {links.map((link) => (
          <li key={link.id}>
            <NavLink
              className="flex min-h-11 items-center border-b border-gray-100 px-2 aria-[current=page]:font-semibold"
              to={link.to}
              onClick={onClose}
              data-testid={`shell.more.${link.id}`}
            >
              {link.label}
            </NavLink>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="min-h-11 rounded border border-gray-300 px-4"
        onClick={onClose}
        data-testid="shell.more.close"
      >
        {t('shell.more.close')}
      </button>
    </Sheet>
  );
}

export function BottomNav({ entries = navEntries() }: { readonly entries?: readonly NavEntry[] }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const org = useStore((state) => state.session.activeOrg, app.store);
  const [more, setMore] = useState(false);
  if (org === null) return null;
  const base = `/o/${encodeURIComponent(org)}`;
  const items = [
    { id: 'home', to: base, label: t('home.nav'), end: true },
    { id: 'tasks', to: `${base}/tasks`, label: t('shell.tasks'), end: true },
    { id: 'notifications', to: '/notifications', label: t('shell.notifications'), end: true },
    { id: 'monitoring', to: `${base}/monitoring`, label: t('monitoring.nav'), end: true },
  ];
  return (
    <nav
      aria-label={t('shell.bottom.label')}
      className="fixed inset-x-0 bottom-0 z-20 border-t border-gray-200 bg-white md:hidden"
      data-testid="shell.bottom"
    >
      <ul className="flex justify-around">
        {items.map((item) => (
          <li key={item.id} className="flex-1">
            <NavLink className={ITEM} to={item.to} end={item.end} data-testid={`shell.bottom.${item.id}`}>
              {item.label}
            </NavLink>
          </li>
        ))}
        <li className="flex-1">
          <button
            type="button"
            className={`${ITEM} w-full`}
            aria-haspopup="dialog"
            aria-expanded={more}
            onClick={() => setMore(true)}
            data-testid="shell.bottom.more"
          >
            {t('shell.more')}
          </button>
        </li>
      </ul>
      {/* outside the bar's stacking context, so the modal sheet covers toasts too */}
      {more && createPortal(<More entries={entries} org={org} onClose={() => setMore(false)} />, document.body)}
    </nav>
  );
}
