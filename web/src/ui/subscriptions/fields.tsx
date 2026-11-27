// Form parts of adding Claude subscriptions on a device that runs logins side by side (KOR-73, ADR-100): the
// subscription's own mailbox, the data of a new account with the consents of the account holder (each unticked, each
// saying that the person whose address it is gave it), the organizations that may use it, and whether the device can
// do it (`useParallel`).
import { useEffect, useState } from 'react';
import { meOrgs } from '../../core/account/session';
import { type Account, isMailbox, type Mailbox, parallelAvailable } from '../../core/credentials/subscriptions';
import { CONSENTS, type Consent, isAccount } from '../../core/requests/bodies';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { OrgName } from './UseEditor';
import '../../core/i18n/lazy/subscription-login';

const INPUT = 'min-h-11 rounded border border-gray-400 px-3';

export interface MailboxDraft {
  readonly host: string;
  readonly port: string;
  readonly user: string;
  readonly password: string;
  readonly email: string;
}

export const EMPTY_MAILBOX: MailboxDraft = { host: '', port: '993', user: '', password: '', email: '' };

/** The mailbox of a draft: `undefined` when it is left empty, `null` when it is incomplete or malformed. */
export function mailboxOf(draft: MailboxDraft): Mailbox | null | undefined {
  const { host, user, password, email } = draft;
  if (host.trim() === '' && user.trim() === '' && password === '' && email.trim() === '') return undefined;
  const found = {
    imap_host: host.trim(),
    imap_port: Number(draft.port.trim()),
    imap_user: user.trim(),
    imap_password: password,
    email: email.trim(),
  };
  return isMailbox(found) ? found : null;
}

export interface AccountDraft {
  readonly on: boolean;
  readonly name: string;
  readonly birth: string;
  readonly consents: readonly Consent[];
}

export const EMPTY_ACCOUNT: AccountDraft = { on: false, name: '', birth: '', consents: [] };

/** The account of a draft: `undefined` when no account is to be created, `null` when the data is malformed. */
export function accountOf(draft: AccountDraft): Account | null | undefined {
  if (!draft.on) return undefined;
  const found: Account = {
    name: draft.name.trim(),
    ...(draft.birth !== '' && { birth_date: draft.birth }),
    consents: draft.consents,
  };
  return isAccount(found) ? found : null;
}

export function MailboxFields({
  id,
  value,
  onChange,
}: {
  readonly id: string;
  readonly value: MailboxDraft;
  readonly onChange: (value: MailboxDraft) => void;
}) {
  const { t } = useI18n();
  const field = (name: keyof MailboxDraft, label: string, type = 'text') => (
    <>
      <label htmlFor={`${id}-${name}`}>{label}</label>
      <input
        id={`${id}-${name}`}
        type={type}
        className={INPUT}
        autoComplete="off"
        value={value[name]}
        onChange={(event) => onChange({ ...value, [name]: event.target.value })}
        data-testid={`${id}.${name}`}
      />
    </>
  );
  return (
    <fieldset className="flex flex-col gap-1" data-testid={`${id}`}>
      <legend>{t('subscription-login.own-mailbox')}</legend>
      <p className="text-sm text-gray-700">{t('subscription-login.own-mailbox.intro')}</p>
      {field('host', t('credentials.mailbox.imap_host'))}
      {field('port', t('credentials.mailbox.imap_port'))}
      {field('user', t('credentials.mailbox.imap_user'))}
      {field('password', t('credentials.mailbox.imap_password'), 'password')}
      {field('email', t('credentials.mailbox.email'), 'email')}
    </fieldset>
  );
}

export function AccountFields({
  id,
  value,
  onChange,
}: {
  readonly id: string;
  readonly value: AccountDraft;
  readonly onChange: (value: AccountDraft) => void;
}) {
  const { t } = useI18n();
  const toggle = (consent: Consent) =>
    onChange({
      ...value,
      consents: value.consents.includes(consent)
        ? value.consents.filter((item) => item !== consent)
        : [...value.consents, consent],
    });
  return (
    <fieldset className="flex flex-col gap-1" data-testid={`${id}`}>
      <label className="flex min-h-11 items-center gap-2">
        <input
          type="checkbox"
          checked={value.on}
          onChange={() => onChange({ ...value, on: !value.on })}
          data-testid={`${id}.on`}
        />
        {t('subscription-login.register')}
      </label>
      {value.on && (
        <>
          <label htmlFor={`${id}-name`}>{t('subscription-login.register.name')}</label>
          <input
            id={`${id}-name`}
            className={INPUT}
            maxLength={100}
            value={value.name}
            onChange={(event) => onChange({ ...value, name: event.target.value })}
            data-testid={`${id}.name`}
          />
          <label htmlFor={`${id}-birth`}>{t('subscription-login.register.birth')}</label>
          <input
            id={`${id}-birth`}
            type="date"
            className={INPUT}
            value={value.birth}
            onChange={(event) => onChange({ ...value, birth: event.target.value })}
            data-testid={`${id}.birth`}
          />
          <p>{t('subscription-login.register.consents')}</p>
          {CONSENTS.map((consent) => (
            <label key={consent} className="flex min-h-11 items-center gap-2">
              <input
                type="checkbox"
                checked={value.consents.includes(consent)}
                onChange={() => toggle(consent)}
                data-testid={`${id}.consent.${consent}`}
              />
              {t(`subscription-login.register.consent.${consent}`)}
            </label>
          ))}
        </>
      )}
    </fieldset>
  );
}

export function OrgChoice({
  id,
  value,
  onChange,
}: {
  readonly id: string;
  readonly value: readonly string[];
  readonly onChange: (value: readonly string[]) => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const profile = useStore((state) => state.me.profile, app.store);
  const orgs = meOrgs(profile).map((item) => item.org_id);
  const toggle = (org: string) =>
    onChange(value.includes(org) ? value.filter((item) => item !== org) : [...value, org]);
  return (
    <fieldset className="flex flex-col gap-1" data-testid={id}>
      <legend>{t('subscription-login.orgs')}</legend>
      {orgs.map((org) => (
        <label key={org} className="flex min-h-11 items-center gap-2">
          <input
            type="checkbox"
            checked={value.includes(org)}
            onChange={() => toggle(org)}
            data-testid={`${id}.${org}`}
          />
          <OrgName org={org} />
        </label>
      ))}
    </fieldset>
  );
}

/** Whether the device that would log a subscription in runs logins side by side: `true` – yes; `null` – no device
 * serves the user yet (the forms offer it, the start places a device and refuses what it cannot do); `false` – no;
 * `undefined` while it is asked. */
export function useParallel(): boolean | null | undefined {
  const { app } = useAppEnv();
  const [parallel, setParallel] = useState<boolean | null | undefined>(undefined);
  useEffect(() => {
    let closed = false;
    // read only: opening a form never places a device
    parallelAvailable(app.tasks).then(
      (found) => !closed && setParallel(found),
      () => !closed && setParallel(false),
    );
    return () => {
      closed = true;
    };
  }, [app]);
  return parallel;
}
