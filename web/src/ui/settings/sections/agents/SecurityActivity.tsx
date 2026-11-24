// "Security activity" of the connected-agents section (11-agent-access.md section 7): the user's account audit
// (`GET /v1/me/audit`) newest first – time, action, result and who: "by you", "by agent <name>" (one of the user's
// own agents, name from its sealed label) or "by agent"; "Load more" pages with the hub's cursor.
//
// Volume (ADR-063 section 1): the rows are a `WindowedList` (`WINDOW` rows at first, `PAGE` per move or loaded page,
// at most `MAX_ROWS` rendered), memoized; the agent names of all rows come from one selector of the list.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { loadAccountAudit } from '../../../../core/agents/activity';
import { formatEventTime, localTimeZone } from '../../../../core/i18n';
import { agentMarker, type AuditRow } from '../../../../core/monitoring/audit';
import { requestErrorCode } from '../../../../core/requests/client';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { type RowAttributes, type RowProps, WindowedList } from '../../../lists/WindowedList';
import { agentNamesSelector, agentsOf, shallowSame } from '../../../screens/audit/select';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';

/** Rows rendered at first, rows added by a move or a loaded page, the cap. */
export const WINDOW = 100;
export const PAGE = 50;
export const MAX_ROWS = 200;

interface Item {
  readonly row: AuditRow;
  /** `agentMarker`: undefined without agent, null for an agent that is not the user's. */
  readonly agent: string | null | undefined;
}

const keyOf = (item: Item): string => String(item.row.seq);

const attributes = (item: Item): RowAttributes => ({
  'data-testid': 'agents.activity.row',
  'data-action': item.row.action,
});

function Row({ item }: RowProps<Item>) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const { row, agent } = item;
  const who =
    agent === undefined
      ? t('agents.activity.by-you')
      : agent === null
        ? t('agents.activity.by-unknown-agent')
        : t('agents.activity.by-agent', { name: agent });
  return (
    <>
      <time className="text-gray-600 tabular-nums" dateTime={row.at}>
        {formatEventTime(Date.parse(row.at), { locale, timeZone: localTimeZone(), now: app.tasks.now() })}
      </time>
      <code className="rounded bg-gray-100 px-1">{row.action}</code>
      <span data-testid="agents.activity.row.result">{t(`audit.result.${row.result}`)}</span>
      <span className={agent === undefined ? '' : 'font-medium text-amber-900'} data-testid="agents.activity.row.who">
        {who}
      </span>
    </>
  );
}

export function SecurityActivity() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [rows, setRows] = useState<readonly AuditRow[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const load = useCallback(
    (before: string | null) => {
      loadAccountAudit(app.client, before).then(
        (page) => {
          setCode(null);
          setLoading(false);
          // "load more" is only offered after a first page
          setRows((known) => (before === null ? page.rows : [...known!, ...page.rows]));
          setNext(page.next);
        },
        (error: unknown) => {
          setLoading(false);
          setCode(requestErrorCode(error));
        },
      );
    },
    [app],
  );
  useEffect(() => load(null), [load]);
  const loaded = useMemo(() => rows ?? [], [rows]);
  const agentIds = useMemo(() => agentsOf(loaded), [loaded]);
  const agentsKey = agentIds.join(' ');
  const agents = useStore(
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `agentsKey` names the agents
    useMemo(() => agentNamesSelector(agentIds), [agentsKey]),
    app.store,
  );
  const items = useMemo(
    () => loaded.map((row): Item => ({ row, agent: agentMarker(row, (id) => agents.get(id)!) })),
    [loaded, agents],
  );
  return (
    <section className="flex flex-col gap-2" aria-labelledby="agents-activity" data-testid="agents.activity">
      <h3 id="agents-activity" className="text-lg font-semibold">
        {t('agents.activity.title')}
      </h3>
      {code !== null && (
        <p role="alert" data-testid="agents.activity.load-failed" data-code={code}>
          {t('agents.activity.load-failed', { reason: reason(code) })}
        </p>
      )}
      {rows !== null && rows.length === 0 && <p data-testid="agents.activity.empty">{t('agents.activity.empty')}</p>}
      <WindowedList
        listId="settings.agents.activity"
        items={items}
        getKey={keyOf}
        Row={Row}
        same={shallowSame}
        newest="start"
        size={WINDOW}
        page={PAGE}
        maxRows={MAX_ROWS}
        rowHeight="sm"
        as="ol"
        className="flex flex-col divide-y divide-gray-200"
        rowClassName="flex flex-wrap items-center gap-2 py-1 text-sm"
        rowAttributes={attributes}
        testId="agents.activity.rows"
        testIds={{ loadMore: 'agents.activity.more' }}
        hasMore={next !== null}
        loading={loading}
        onLoadMore={() => {
          setLoading(true);
          load(next);
        }}
      />
    </section>
  );
}
