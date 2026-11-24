// "Security activity" of the connected-agents section (11-agent-access.md section 7): the user's account audit
// (`GET /v1/me/audit`) newest first – time, action, result and who: "by you", "by agent <name>" (one of the user's
// own agents, name from its sealed label) or "by agent"; "Load more" pages with the hub's cursor.
import { useCallback, useEffect, useState } from 'react';
import { loadAccountAudit } from '../../../../core/agents/activity';
import { formatEventTime } from '../../../../core/i18n';
import { agentMarker, type AuditRow } from '../../../../core/monitoring/audit';
import { requestErrorCode } from '../../../../core/requests/client';
import { agentName } from '../../../../core/store/slices/agents';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';

function Row({ row }: { readonly row: AuditRow }) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const agent = useStore((state) => agentMarker(row, (id) => agentName(state, id)), app.store);
  const who =
    agent === undefined
      ? t('agents.activity.by-you')
      : agent === null
        ? t('agents.activity.by-unknown-agent')
        : t('agents.activity.by-agent', { name: agent });
  return (
    <li
      className="flex flex-wrap items-center gap-2 py-1 text-sm"
      data-testid="agents.activity.row"
      data-action={row.action}
    >
      <time className="text-gray-600 tabular-nums" dateTime={row.at}>
        {formatEventTime(Date.parse(row.at), { locale, timeZone, now: app.tasks.now() })}
      </time>
      <code className="rounded bg-gray-100 px-1">{row.action}</code>
      <span data-testid="agents.activity.row.result">{t(`audit.result.${row.result}`)}</span>
      <span className={agent === undefined ? '' : 'font-medium text-amber-900'} data-testid="agents.activity.row.who">
        {who}
      </span>
    </li>
  );
}

export function SecurityActivity() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [rows, setRows] = useState<readonly AuditRow[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const load = useCallback(
    (before: string | null) => {
      loadAccountAudit(app.client, before).then(
        (page) => {
          setCode(null);
          // "load more" is only offered after a first page
          setRows((known) => (before === null ? page.rows : [...known!, ...page.rows]));
          setNext(page.next);
        },
        (error: unknown) => setCode(requestErrorCode(error)),
      );
    },
    [app],
  );
  useEffect(() => load(null), [load]);
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
      {rows !== null && rows.length > 0 && (
        <ol className="flex flex-col divide-y divide-gray-200" data-testid="agents.activity.rows">
          {rows.map((row) => (
            <Row key={row.seq} row={row} />
          ))}
        </ol>
      )}
      {next !== null && (
        <button
          type="button"
          className="min-h-11 self-start underline"
          onClick={() => load(next)}
          data-testid="agents.activity.more"
        >
          {t('audit.more')}
        </button>
      )}
    </section>
  );
}
