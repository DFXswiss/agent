// Shared parts of the GitHub identity views (ADR-078, 06-web.md section 8.1): `useIdentities` loads the user's
// identities once per sign-in (the `githubIdentities` slice, `core/github/identities.ts`) and offers a reload;
// `useIdentityText` names an identity – an own account by its login (and display name), a company account as
// "Company account of <org> · connected <date>" with the login only when this browser stored it (company records are
// sealed to the organization's devices, KM-35) – and `IdentityName` renders the name of an identity of a task. Texts
// from GitHub (login, name) are rendered as text only.
import { useCallback, useEffect, useState } from 'react';
import { type GithubIdentity, identityName, loadIdentities } from '../../../../core/github/identities';
import type { GithubIdentitiesState } from '../../../../core/store/slices/githubIdentities';
import { requestErrorCode } from '../../../../core/requests/client';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';

export interface IdentitiesView {
  readonly state: GithubIdentitiesState;
  /** The code of the last failed load, null otherwise. */
  readonly error: string | null;
  readonly reload: () => void;
}

/** The identities of the user, loaded again whenever a view that shows them opens (another member or admin may have
 * changed them meanwhile; there is no live frame for identity records). */
export function useIdentities(): IdentitiesView {
  const { app } = useAppEnv();
  const state = useStore((current) => current.githubIdentities, app.store);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    void loadIdentities(app.tasks)
      .then(() => null, requestErrorCode)
      .then(setError);
  }, [app]);
  useEffect(reload, [reload]);
  return { state, error, reload };
}

/** The date of a time in the active locale. */
export function useDate(): (at: string) => string {
  const { locale } = useI18n();
  return (at) => new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(Date.parse(at));
}

/** The text of an identity (module comment); `orgName` names the organization of a company account. */
export function useIdentityText(
  knownLogins: Readonly<Record<string, string>>,
  orgName: string | null,
): (identity: GithubIdentity) => string {
  const { t } = useI18n();
  const date = useDate();
  return (identity) => {
    if (identity.status === 'verified') {
      const { login, name } = identity.account;
      return name === null ? login : t('identities.named', { login, name });
    }
    if (identity.status === 'unverifiable') return t('identities.unverifiable', { code: identity.code });
    const org = orgName ?? t('identities.org.unknown');
    const base = t('identities.company', { org, date: date(identity.createdAt) });
    const login = knownLogins[identity.id];
    return login === undefined ? base : t('identities.company.login', { label: base, login });
  };
}

/** The name of identity `id` in a task of an organization named `orgName` ("another account" when not readable). */
export function IdentityName({ id, orgName }: { readonly id: string; readonly orgName: string | null }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const state = useStore((current) => current.githubIdentities, app.store);
  const name = identityName(state.identities, state.knownLogins, id);
  const org = orgName ?? t('identities.org.unknown');
  let text: string;
  if (name.kind === 'login') text = name.login;
  else if (name.kind === 'company' && name.login !== null)
    text = t('identities.as-company.login', { org, login: name.login });
  else if (name.kind === 'company') text = t('identities.as-company', { org });
  else text = t('identities.other');
  return (
    <span data-testid="identities.name" data-kind={name.kind}>
      {text}
    </span>
  );
}
