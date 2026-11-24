// The workgroups of the active org in the sidebar from `lg` (06-web.md section 12.1): the ones the user can read, by
// name (verified `wg.profile`), linking to their task lists; the one in view is marked.
import { Link, matchPath, useLocation } from 'react-router-dom';
import { workgroupRows } from '../../core/org/workgroups';
import { useI18n } from '../i18n';
import { useDirectory, useOrgView } from '../screens/tasks/hooks';

export function SidebarWorkgroups({ org }: { readonly org: string }) {
  const { t } = useI18n();
  const location = useLocation();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  if (state === null || userId === null) return null;
  const current = matchPath('/o/:org/w/:wg/*', location.pathname)?.params.wg ?? null;
  const rows = workgroupRows(state, userId, directory).filter((row) => row.myAccess !== null && !row.archived);
  return (
    <section
      aria-labelledby="sidebar-workgroups"
      className="flex flex-col gap-1"
      data-testid="shell.sidebar.workgroups"
    >
      <h2 id="sidebar-workgroups" className="px-2 text-xs font-semibold uppercase text-gray-700">
        {t('shell.sidebar.workgroups')}
      </h2>
      <ul>
        {rows.map((row) => (
          <li key={row.id}>
            <Link
              className="flex min-h-11 items-center rounded px-2 hover:bg-gray-50 aria-[current=page]:bg-blue-50 aria-[current=page]:font-semibold"
              to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(row.id)}`}
              aria-current={row.id === current ? 'page' : undefined}
              data-testid="shell.sidebar.workgroup"
              data-wg={row.id}
            >
              {row.name ?? t('shell.sidebar.unnamed')}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
