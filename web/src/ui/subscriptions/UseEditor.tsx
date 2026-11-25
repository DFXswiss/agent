// The use of a further subscription (KOR-69, ADR-090 section 3): "only measure" (the default) or "for sessions of"
// the organizations of the user (and, for a user with the platform role `operator`, the operator's pool). Saved as the
// sealed use of the record (`setSubscriptionUse`); nobody else's session uses the subscription otherwise.
import { useState } from 'react';
import { meOrgs } from '../../core/account/session';
import { type SubscriptionUse, setSubscriptionUse } from '../../core/credentials/subscriptions';
import { requestErrorCode } from '../../core/requests/client';
import { orgEntry } from '../../core/store/slices/orgs';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useDirectory } from '../screens/tasks/hooks';
import { PRIMARY } from '../settings/sections/credentials/DevicePicker';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';

export const OPERATOR_ROLE = 'operator';

function OrgName({ org }: { readonly org: string }) {
  const { app } = useAppEnv();
  const verified = useStore((state) => orgEntry(state.orgs, org).log !== null, app.store);
  const directory = useDirectory(org, verified);
  return <>{directory.orgName ?? org.slice(0, 8)}</>;
}

export function UseEditor({
  id,
  use,
  onSaved,
}: {
  readonly id: string;
  readonly use: SubscriptionUse;
  readonly onSaved: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const profile = useStore((state) => state.me.profile, app.store);
  const platform = useStore((state) => state.orgs.platform?.state ?? null, app.store);
  const userId = useStore((state) => state.me.userId, app.store);
  const operator = platform !== null && userId !== null && platform.hasRole(userId, OPERATOR_ROLE);
  const orgs = meOrgs(profile).map((item) => item.org_id);
  const [shared, setShared] = useState(use.pool || use.orgs.length > 0);
  const [pool, setPool] = useState(use.pool);
  const [chosen, setChosen] = useState<readonly string[]>(use.orgs);
  const [state, setState] = useState<{ readonly kind: 'idle' | 'saving' | 'saved' } | { kind: 'error'; code: string }>({
    kind: 'idle',
  });
  const toggle = (org: string) =>
    setChosen((current) => (current.includes(org) ? current.filter((item) => item !== org) : [...current, org]));
  const save = async () => {
    setState({ kind: 'saving' });
    try {
      await setSubscriptionUse(app.tasks, id, shared ? { pool, orgs: chosen } : null);
      setState({ kind: 'saved' });
      onSaved();
    } catch (error) {
      setState({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  return (
    <fieldset className="flex flex-col gap-1" data-testid="subuse">
      <legend>{t('subscription-login.use.label')}</legend>
      <label className="flex min-h-11 items-center gap-2">
        <input
          type="radio"
          name={`subuse-${id}`}
          checked={!shared}
          onChange={() => setShared(false)}
          data-testid="subuse.measure"
        />
        {t('subscription-login.use.measure')}
      </label>
      <label className="flex min-h-11 items-center gap-2">
        <input
          type="radio"
          name={`subuse-${id}`}
          checked={shared}
          onChange={() => setShared(true)}
          data-testid="subuse.shared"
        />
        {t('subscription-login.use.shared')}
      </label>
      {shared && (
        <div className="ml-6 flex flex-col gap-1">
          {operator && (
            <label className="flex min-h-11 items-center gap-2">
              <input type="checkbox" checked={pool} onChange={() => setPool(!pool)} data-testid="subuse.pool" />
              {t('subscription-login.use.pool')}
            </label>
          )}
          {orgs.map((org) => (
            <label key={org} className="flex min-h-11 items-center gap-2">
              <input
                type="checkbox"
                checked={chosen.includes(org)}
                onChange={() => toggle(org)}
                data-testid={`subuse.org.${org}`}
              />
              <OrgName org={org} />
            </label>
          ))}
        </div>
      )}
      <button
        type="button"
        className={PRIMARY}
        disabled={state.kind === 'saving'}
        onClick={() => void save()}
        data-testid="subuse.save"
      >
        {t('subscription-login.use.save')}
      </button>
      {state.kind === 'saved' && (
        <p role="status" data-testid="subuse.saved">
          {t('subscription-login.saved')}
        </p>
      )}
      {state.kind === 'error' && (
        <p role="alert" data-testid="subuse.failed" data-code={state.code}>
          {t('subscription-login.save-failed', { reason: reason(state.code) })}
        </p>
      )}
    </fieldset>
  );
}
