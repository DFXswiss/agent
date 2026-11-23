// Org switcher (06-web.md section 7.5): a header control listing every org of `GET /v1/me` with its decrypted name
// (`org.profile`, verified); choosing one navigates to `/o/:org`, which makes it the active org and reconnects the live
// channel with the new `org` parameter (`AppSession.activate`). "Create organization" is available to every user: a
// non-private genesis with a new default workgroup (`prepareOrg`, `POST /v1/orgs`); such an org cannot use platform
// devices until the operator grants platform access (ADR-026), which the dialog says. Private organizations are only
// created from a platform invitation.
import { useId, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { createOrg, displayName, prepareOrg, type PreparedOrg } from '../../../core/account/genesis';
import { meOrgs } from '../../../core/account/session';
import { selectOwnName } from '../../../core/org/members';
import { orgEntry } from '../../../core/store/slices/orgs';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../context';
import { ActionDialog } from '../../screens/members/ActionDialog';
import { useDirectory } from '../../screens/tasks/hooks';

function OrgName({ org, fallback }: { readonly org: string; readonly fallback: string }) {
  const { app } = useAppEnv();
  const verified = useStore((state) => orgEntry(state.orgs, org).log !== null, app.store);
  const directory = useDirectory(org, verified);
  return <>{directory.orgName ?? fallback}</>;
}

function CreateOrgDialog({ onClose }: { readonly onClose: () => void }) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const navigate = useNavigate();
  const ids = { name: useId(), display: useId() };
  const known = useStore(selectOwnName(app.store.getState().session.activeOrg, app.userId), app.store);
  const [name, setName] = useState('');
  // the own name follows the store until the user edits the field: the dialog may open before the profiles of the
  // active org are read (right after sign-in), and a name copied once would stay empty
  const [typed, setTyped] = useState<string | null>(null);
  const display = typed ?? known;
  const created = useRef('');
  // prepared once: a retry (lost answer, failed install) sends the identical body and the hub answers `replayed`;
  // a changed name prepares a new org
  const prepared = useRef<{ key: string; org: PreparedOrg } | null>(null);
  const run = async () => {
    const { identity, userId } = app.store.getState().me;
    setTyped(display); // pinned from the first attempt: a retry must send the same prepared org
    const key = `${name}\n${display}`;
    if (prepared.current?.key !== key)
      prepared.current = {
        key,
        org: await prepareOrg({
          userId: userId!,
          identity: identity!,
          displayName: display,
          orgName: name,
          locale,
          now: app.tasks.now,
        }),
      };
    const org = prepared.current.org;
    await createOrg((method, target, options) => app.client.request(method, target, options), org);
    await app.installOrg(org);
    created.current = org.org;
  };
  return (
    <ActionDialog
      title={t('orgs.create.title')}
      testId="org-switcher.create"
      confirmLabel={t('orgs.create.confirm')}
      ready={displayName(name) !== null && displayName(display) !== null}
      run={run}
      onDone={() => {
        onClose();
        navigate(`/o/${encodeURIComponent(created.current)}`);
      }}
      onClose={onClose}
    >
      <label htmlFor={ids.name} className="font-medium">
        {t('orgs.create.name')}
      </label>
      <input
        id={ids.name}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={name}
        maxLength={200}
        onChange={(event) => setName(event.target.value)}
        data-testid="org-switcher.create.name"
      />
      <label htmlFor={ids.display} className="font-medium">
        {t('orgs.create.display-name')}
      </label>
      <input
        id={ids.display}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={display}
        maxLength={200}
        onChange={(event) => setTyped(event.target.value)}
        data-testid="org-switcher.create.display-name"
      />
      <p role="note" className="text-sm text-gray-700" data-testid="org-switcher.create.platform-note">
        {t('orgs.create.platform-note')}
      </p>
    </ActionDialog>
  );
}

export function OrgSwitcher() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const profile = useStore((state) => state.me.profile, app.store);
  const active = useStore((state) => state.session.activeOrg, app.store);
  const [creating, setCreating] = useState(false);
  const [openKey, setOpenKey] = useState(0);
  const orgs = meOrgs(profile);
  const close = () => setOpenKey((key) => key + 1);
  return (
    <>
      <details key={openKey} className="relative" data-testid="org-switcher">
        <summary className="flex min-h-11 cursor-pointer items-center px-2" data-testid="org-switcher.current">
          {active === null ? t('orgs.switcher.label') : <OrgName org={active} fallback={t('orgs.unnamed')} />}
        </summary>
        <div className="absolute right-0 z-10 flex w-64 flex-col gap-1 rounded border border-gray-200 bg-white p-2 shadow">
          <ul aria-label={t('orgs.switcher.label')} data-testid="org-switcher.list">
            {orgs.map((org) => (
              <li key={org.org_id}>
                <Link
                  className="flex min-h-11 items-center px-2 aria-[current=true]:font-semibold"
                  to={`/o/${encodeURIComponent(org.org_id)}`}
                  aria-current={org.org_id === active}
                  onClick={close}
                  data-testid="org-switcher.item"
                  data-org={org.org_id}
                >
                  <OrgName org={org.org_id} fallback={t('orgs.unnamed')} />
                  {org.private && <span className="ml-2 text-sm text-gray-600">{t('orgs.private')}</span>}
                </Link>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="min-h-11 px-2 text-left underline"
            onClick={() => {
              close();
              setCreating(true);
            }}
            data-testid="org-switcher.create.open"
          >
            {t('orgs.create.open')}
          </button>
        </div>
      </details>
      {creating && <CreateOrgDialog onClose={() => setCreating(false)} />}
    </>
  );
}

export const headerItem = { id: 'org-switcher', order: 10, Component: OrgSwitcher };
