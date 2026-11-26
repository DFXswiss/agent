// The working modes of a task in a task list and the situation view (ADR-098 section 3): small chips with the titles
// in the user's language (the name while the catalogue is not known), shown only when the task has any. Spans with list
// roles: the chips sit inside the row's text column (phrasing content, inside a link in the task lists).
import type { TaskRow } from '../../core/store/slices/tasks';
import { modeTitle, taskModes } from '../../core/modes/modes';
import { useI18n } from '../i18n';
import { useModeCatalogue } from './catalogue';

export function ModeChips({ row, testId }: { readonly row: TaskRow; readonly testId: string }) {
  const { t, locale } = useI18n();
  const modes = taskModes(row);
  const { catalogue } = useModeCatalogue(modes.length > 0, false);
  if (modes.length === 0) return null;
  return (
    <span role="list" className="flex flex-wrap gap-1" aria-label={t('modes.list.label')} data-testid={testId}>
      {modes.map((item) => (
        <span
          key={item.mode}
          role="listitem"
          className="rounded-full bg-indigo-50 px-2 text-xs text-indigo-900"
          data-testid={`${testId}.mode`}
          data-mode={item.mode}
        >
          {modeTitle(catalogue, item.mode, locale)}
        </span>
      ))}
    </span>
  );
}
