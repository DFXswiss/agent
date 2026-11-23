// A tab bar whose active tab is the `tab` search parameter (`?tab=sessions`), so a tab is a link that survives a reload
// and the back button; the first tab is the default. Used by the task page and the session detail.
import { Link, useLocation } from 'react-router-dom';

export interface TabLink {
  readonly id: string;
  readonly label: string;
}

/** The active tab: the `tab` parameter when it names one of `tabs`, else the first. */
export function activeTab(search: string, tabs: readonly TabLink[]): string {
  const wanted = new URLSearchParams(search).get('tab');
  return tabs.find((tab) => tab.id === wanted)?.id ?? tabs[0]!.id;
}

export function Tabs({
  tabs,
  label,
  testId,
}: {
  readonly tabs: readonly TabLink[];
  readonly label: string;
  readonly testId: string;
}) {
  const location = useLocation();
  const active = activeTab(location.search, tabs);
  return (
    <nav aria-label={label} className="flex gap-1 overflow-x-auto border-b border-gray-200" data-testid={testId}>
      {tabs.map((tab, index) => (
        <Link
          key={tab.id}
          to={{ pathname: location.pathname, search: index === 0 ? '' : `?tab=${tab.id}` }}
          aria-current={tab.id === active ? 'page' : undefined}
          className={`flex min-h-11 items-center px-3 ${tab.id === active ? 'border-b-2 border-blue-700 font-medium' : 'text-gray-600'}`}
          data-testid={`${testId}.${tab.id}`}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}
