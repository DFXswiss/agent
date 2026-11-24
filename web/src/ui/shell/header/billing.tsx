// Header item "Credit" (12-billing.md section 9, ADR-062 section 7): the active organization's balance, in red with
// "Credit used up" when it is exhausted; a link to the section "Credit and usage". Read when the organization changes
// and every `REFRESH_MS`; hidden while billing is inactive or the hub has no billing (an older release).
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { formatUsd, loadOrgBilling, type OrgBilling } from '../../../core/billing/billing';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../context';

export const REFRESH_MS = 60_000;

export function CreditBadge() {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const org = useStore((state) => state.session.activeOrg, app.store);
  const [billing, setBilling] = useState<OrgBilling | null>(null);
  useEffect(() => {
    if (org === null) return undefined;
    let live = true;
    const read = () => {
      loadOrgBilling(app.tasks, org).then(
        (found) => live && setBilling(found),
        () => live && setBilling(null),
      );
    };
    read();
    const timer = setInterval(read, REFRESH_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [app, org]);
  if (billing === null || !billing.active) return null;
  return (
    <Link
      to="/settings#billing"
      className={`min-h-11 px-2 py-2 ${billing.exhausted ? 'font-semibold text-red-700' : ''}`}
      data-testid="credit.badge"
      data-exhausted={billing.exhausted ? 'yes' : 'no'}
    >
      {billing.exhausted
        ? t('billing.exhausted.header')
        : t('billing.balance.header', { amount: formatUsd(billing.balanceMicroUsd, locale) })}
    </Link>
  );
}

export const headerItem = { id: 'credit', order: 15, Component: CreditBadge };
