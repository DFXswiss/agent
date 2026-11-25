// The organization a settings section of the active org works on (KOR-68): its name, and a choice in place when the
// user is a member of several. Choosing one makes it the active org (its live channel runs) and the last one used on
// this browser. Without any org the line says so instead of waiting for one.
import { useEffect, useId } from 'react';
import { meOrgs } from '../../core/account/session';
import { orgEntry } from '../../core/store/slices/orgs';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { useDirectory } from '../screens/tasks/hooks';

function OrgOption({ org, private: own }: { readonly org: string; readonly private: boolean }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const verified = useStore((state) => orgEntry(state.orgs, org).log !== null, app.store);
  const name = useDirectory(org, verified).orgName ?? t('orgs.unnamed');
  return <option value={org}>{own ? `${name} (${t('orgs.private')})` : name}</option>;
}

export function ActiveOrg({ testId }: { readonly testId: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const id = useId();
  const profile = useStore((state) => state.me.profile, app.store);
  const active = useStore((state) => state.session.activeOrg, app.store);
  const orgs = meOrgs(profile);
  const missing = orgs.filter((org) => orgEntry(app.store.getState().orgs, org.org_id).log === null);
  const unread = missing.map((org) => org.org_id).join(' ');
  useEffect(() => {
    // the names come from the verified logs; a page opened directly has not read them yet
    for (const org of unread.split(' ').filter(Boolean)) app.tasks.refreshLog(org).catch(() => false);
  }, [app, unread]);
  if (orgs.length === 0)
    return (
      <p className="text-sm text-gray-600" data-testid={`${testId}.none`}>
        {t('settings.no-org')}
      </p>
    );
  const choose = (org: string) => {
    app.activate(org);
    app.rememberOrg(org);
  };
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid={testId}>
      <label htmlFor={id} className="font-medium">
        {t('settings.org.label')}
      </label>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={active ?? ''}
        onChange={(event) => choose(event.target.value)}
        data-testid={`${testId}.select`}
      >
        {active === null && <option value="">{t('settings.org.choose')}</option>}
        {orgs.map((org) => (
          <OrgOption key={org.org_id} org={org.org_id} private={org.private} />
        ))}
      </select>
    </div>
  );
}
