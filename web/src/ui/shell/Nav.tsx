// Navigation built from the discovered entries (`screens/<area>/nav.ts`, ADR-027 section 4): `:org` is filled with
// the active org; entries that need an org are hidden while none is active. Layout of 06-web.md section 12.1: below
// `md` the bottom navigation (`ui/responsive/BottomNav.tsx`) replaces it; from `md` it is a row under the header; from
// `lg` a left sidebar with the entries first (Home and Tasks on top, KOR-32) and the workgroups of the active org
// below them. An entry is marked as the current page only on its own path (Home is not current on every org page).
// Entries may hide themselves or show a badge (`NavEntry.hidden`/`badge`, e.g. the community's unread count).
import { NavLink } from 'react-router-dom';
import { badgeText, type NavEntry, navEntries, useNavState } from '../nav';
import { SidebarWorkgroups } from '../responsive/Workgroups';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from './context';

/** The link target of an entry for `org`, or null if it needs an org and there is none. */
export function navTarget(entry: NavEntry, org: string | null): string | null {
  if (!entry.to.includes(':org')) return entry.to;
  return org === null ? null : entry.to.replace(':org', encodeURIComponent(org));
}

export function Nav({ entries = navEntries() }: { readonly entries?: readonly NavEntry[] }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const org = useStore((state) => state.session.activeOrg, app.store);
  const { visible, badges } = useNavState(entries, app.store);
  const links = visible.flatMap((entry) => {
    const to = navTarget(entry, org);
    return to === null ? [] : [{ id: entry.id, to, label: t(entry.labelKey) }];
  });
  if (links.length === 0) return null;
  return (
    <nav
      aria-label={t('shell.nav.label')}
      className="hidden border-gray-200 bg-white md:block md:border-b lg:w-60 lg:shrink-0 lg:border-b-0 lg:border-r lg:p-2"
      data-testid="shell.nav"
    >
      <ul className="flex flex-wrap gap-x-4 px-4 lg:flex-col lg:gap-0 lg:px-0">
        {links.map((link) => (
          <li key={link.id}>
            <NavLink
              className="flex min-h-11 min-w-11 items-center px-2 aria-[current=page]:font-semibold lg:rounded lg:hover:bg-gray-50"
              to={link.to}
              end
              data-testid={`shell.nav.${link.id}`}
            >
              {link.label}
              <NavBadge count={badges.get(link.id)!} testId={`shell.nav.${link.id}.badge`} />
            </NavLink>
          </li>
        ))}
      </ul>
      <div className="hidden lg:mt-4 lg:block">{org !== null && <SidebarWorkgroups org={org} />}</div>
    </nav>
  );
}

/** The badge of a navigation entry (nothing at 0). */
export function NavBadge({ count, testId }: { readonly count: number; readonly testId: string }) {
  const text = badgeText(count);
  if (text === null) return null;
  return (
    <span className="ml-2 min-w-5 rounded-full bg-red-700 px-1.5 text-center text-xs text-white" data-testid={testId}>
      {text}
    </span>
  );
}
