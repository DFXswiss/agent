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

/** The look of a tab bar: on a light page, or slim on the dark page of "Claude Code" (KONZEPT E-43). */
const TONES = {
  light: {
    bar: 'flex gap-1 overflow-x-auto border-b border-gray-200',
    tab: 'flex min-h-11 items-center px-3',
    active: 'border-b-2 border-blue-700 font-medium',
    other: 'text-gray-600',
  },
  dark: {
    bar: 'flex gap-1 overflow-x-auto',
    tab: 'flex min-h-11 items-center whitespace-nowrap px-2',
    active: 'text-white',
    other: 'text-gray-400 hover:text-gray-200',
  },
} as const;

export function Tabs({
  tabs,
  label,
  testId,
  tone = 'light',
}: {
  readonly tabs: readonly TabLink[];
  readonly label: string;
  readonly testId: string;
  readonly tone?: keyof typeof TONES;
}) {
  const location = useLocation();
  const active = activeTab(location.search, tabs);
  const look = TONES[tone];
  return (
    <nav aria-label={label} className={look.bar} data-testid={testId}>
      {tabs.map((tab, index) => (
        <Link
          key={tab.id}
          to={{ pathname: location.pathname, search: index === 0 ? '' : `?tab=${tab.id}` }}
          aria-current={tab.id === active ? 'page' : undefined}
          className={`${look.tab} ${tab.id === active ? look.active : look.other}`}
          data-testid={`${testId}.${tab.id}`}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}
