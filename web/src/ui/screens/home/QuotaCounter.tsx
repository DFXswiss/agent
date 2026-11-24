// The counter of the organization's subscriptions in the head of the situation view (ADR-081 section 6): "Subscriptions:
// n available · m used up – next release 14:05", a link to the settings section "Model access and quotas". Read when
// the organization changes and every `REFRESH_MS`; hidden while the organization has no subscription it could list and
// when the hub cannot answer (an older release has no such route).
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { loadOrgQuotas, type OrgQuotas } from '../../../core/credentials/orgQuotas';
import { useAppEnv } from '../../shell/context';
import { SummaryText } from '../../settings/sections/model-quotas/summary';

export const REFRESH_MS = 60_000;

export function QuotaCounter({ org }: { readonly org: string }) {
  const { app } = useAppEnv();
  const [summary, setSummary] = useState<OrgQuotas['summary'] | null>(null);
  useEffect(() => {
    let live = true;
    const read = () => {
      loadOrgQuotas(app.tasks, org).then(
        (found) => live && setSummary(found.summary),
        () => live && setSummary(null),
      );
    };
    read();
    const timer = setInterval(read, REFRESH_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [app, org]);
  if (summary === null || summary.usable + summary.exhausted === 0) return null;
  return (
    <Link
      to="/settings#model-quotas"
      className={`flex min-h-11 items-center self-start text-sm underline ${summary.usable === 0 ? 'text-red-800' : 'text-gray-700'}`}
      data-testid="attention.quotas"
      data-usable={summary.usable}
      data-exhausted={summary.exhausted}
    >
      <SummaryText summary={summary} />
    </Link>
  );
}
