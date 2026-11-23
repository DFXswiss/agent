// Navigation built from the discovered entries (`screens/<area>/nav.ts`, ADR-027 section 4): `:org` is filled with
// the active org; entries that need an org are hidden while none is active. Layout of 06-web.md section 12.1: below
// `md` the bottom navigation (`ui/responsive/BottomNav.tsx`) replaces it; from `md` it is a row under the header; from
// `lg` a left sidebar with the workgroups of the active org above the entries.
import { NavLink } from 'react-router-dom';
import { type NavEntry, navEntries } from '../nav';
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
  const links = entries.flatMap((entry) => {
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
      <div className="hidden lg:mb-4 lg:block">{org !== null && <SidebarWorkgroups org={org} />}</div>
      <ul className="flex flex-wrap gap-x-4 px-4 lg:flex-col lg:gap-0 lg:px-0">
        {links.map((link) => (
          <li key={link.id}>
            <NavLink
              className="flex min-h-11 min-w-11 items-center px-2 aria-[current=page]:font-semibold lg:rounded lg:hover:bg-gray-50"
              to={link.to}
              data-testid={`shell.nav.${link.id}`}
            >
              {link.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
