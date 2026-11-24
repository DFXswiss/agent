// Tab "Costs" of a task (12-billing.md sections 6 and 9, ADR-062 section 7): the charges of the task per session -
// model tokens and VM runtime - the total and, when the hub sends it, the consumption per person (ADR-083)
// (`GET /v1/tasks/{task}/charges`), read when the tab opens. The sessions
// are a `WindowedList` (ADR-086 section 1) over the hub's list read page by page, newest first ("Load more",
// `usePagedList`); a hub without paging answers the whole list and says when its cap cut it.
import { useCallback, useState } from 'react';
import {
  formatRuntime,
  formatUsd,
  loadTaskChargesPage,
  type PersonConsumption,
  type SessionCharges,
} from '../../../core/billing/billing';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useI18n } from '../../i18n';
import { type RowProps, WindowedList } from '../../lists/WindowedList';
import { MAX_ROWS, PAGE, sameValue } from '../../screens/members/lists';
import { PagedNotes } from '../../screens/members/PagedNotes';
import { usePagedList } from '../../screens/members/usePagedList';
import { useAppEnv } from '../../shell/context';
import { useReason } from '../../settings/sections/github/reason';
import { PeopleUsage } from '../people';

const NO_ROWS: readonly SessionCharges[] = [];

const sessionKey = (row: SessionCharges) => row.sessionId;

function SessionRow({ item: row }: RowProps<SessionCharges>) {
  const { t, locale } = useI18n();
  return (
    <>
      {t('billing.task.session', {
        session: row.sessionId.slice(0, 8),
        tokens: formatUsd(row.tokensMicroUsd, locale),
        count: row.tokens.toLocaleString(locale),
        runtime: formatUsd(row.runtimeMicroUsd, locale),
        duration: formatRuntime(row.runtimeMs),
      })}
    </>
  );
}

export function ChargesTab({ orgId, taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const reason = useReason();
  // the total of the whole task, from its newest page (read before the rows are shown)
  const [total, setTotal] = useState(0);
  const [people, setPeople] = useState<readonly PersonConsumption[] | null>(null);
  const [reload, setReload] = useState(0);
  const read = useCallback(
    async (before: string | null) => {
      const page = await loadTaskChargesPage(app.tasks, taskId, before);
      if (before === null) {
        setTotal(page.totalMicroUsd!);
        setPeople(page.people);
      }
      return page;
    },
    [app, taskId],
  );
  const list = usePagedList(read, sessionKey, reload);
  const rows = list.rows ?? NO_ROWS;
  return (
    <div className="flex flex-col gap-3 p-2" data-testid="charges.tab">
      {list.code !== null && (
        <p role="alert" data-testid="charges.load-failed" data-code={list.code}>
          {t('billing.load-failed', { reason: reason(list.code) })}
        </p>
      )}
      {list.rows !== null && rows.length === 0 && !list.more && (
        <p data-testid="charges.empty">{t('billing.task.empty')}</p>
      )}
      {rows.length > 0 && (
        <p className="font-semibold" data-testid="charges.total">
          {t('billing.task.total', { amount: formatUsd(total, locale) })}
        </p>
      )}
      <PagedNotes
        list={list}
        shown={rows.length}
        note="billing.task.truncated"
        rows="charges.sessions"
        testId="charges"
      />
      {rows.length > 0 && (
        <WindowedList
          listId="task.charges"
          items={rows}
          getKey={sessionKey}
          Row={SessionRow}
          same={sameValue}
          newest="start"
          page={PAGE}
          maxRows={MAX_ROWS}
          testId="charges.sessions"
          className="flex flex-col gap-1"
          rowHeight="sm"
          hasMore={list.paging.hasMore}
          loading={list.paging.loading}
          onLoadMore={list.paging.loadMore}
        />
      )}
      {rows.length > 0 && people !== null && (
        <section>
          <h3 className="font-semibold">{t('billing.task.people')}</h3>
          <PeopleUsage org={orgId} rows={people} testId="charges.people" />
        </section>
      )}
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={() => setReload((n) => n + 1)}
        data-testid="charges.reload"
      >
        {t('billing.reload')}
      </button>
    </div>
  );
}
