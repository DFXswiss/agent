// "Add a subscription" (KOR-69, ADR-090): a label (and an optional note), one button – the login on a platform device
// starts at once (`SubscriptionLogin`) and the new subscription is only measured until its use is switched on.
// KOR-73 (ADR-099): with a device that runs logins side by side (`useParallel`) the form also takes the subscription's
// own mailbox (then the login is automatic and the record is written first), the data of a new account with the
// account holder's consents, and the organizations that may use it; otherwise it stays the single login of KOR-69.
import { type FormEvent, useState } from 'react';
import { type LoginInput, newSubscriptionId } from '../../core/credentials/subscriptions';
import { EMAIL_RE, isLabel } from '../../core/requests/bodies';
import { useI18n } from '../i18n';
import { BUTTON, PRIMARY } from '../settings/sections/credentials/DevicePicker';
import {
  AccountFields,
  type AccountDraft,
  accountOf,
  EMPTY_ACCOUNT,
  EMPTY_MAILBOX,
  MailboxFields,
  type MailboxDraft,
  mailboxOf,
  OrgChoice,
  useParallel,
} from './fields';
import { SubscriptionLogin } from './SubscriptionLogin';
import '../../core/i18n/lazy/subscription-login';

const INPUT = 'min-h-11 rounded border border-gray-400 px-3';

const emailOk = (email: string) => email.trim() === '' || (EMAIL_RE.test(email.trim()) && email.trim().length <= 254);

/** The login input of the form (`null` while it is not valid). */
export function addInput(
  id: string,
  form: {
    readonly name: string;
    readonly note: string;
    readonly automatic: boolean;
    readonly email: string;
    readonly mailbox: MailboxDraft;
    readonly account: AccountDraft;
    readonly orgs: readonly string[];
  },
  parallel: boolean,
): LoginInput | null {
  const { name, note, email } = form;
  if (!isLabel(name.trim(), 64) || !(note.trim() === '' || isLabel(note.trim(), 500)) || !emailOk(email)) return null;
  if (!parallel) {
    return form.automatic
      ? { subscriptionId: id, mode: 'automatic', name, note, email }
      : { subscriptionId: id, mode: 'manual', name, note };
  }
  const mailbox = mailboxOf(form.mailbox);
  // registration data count only for an automatic login with its own mailbox
  const account = mailbox === undefined ? undefined : accountOf(form.account);
  if (mailbox === null || account === null) return null;
  const create = {
    name,
    note,
    email,
    orgs: form.orgs,
    ...(mailbox !== undefined && { mailbox }),
  };
  const mode = mailbox === undefined ? ('manual' as const) : ('automatic' as const);
  return {
    subscriptionId: id,
    mode,
    create,
    ...(mode === 'automatic' && email.trim() !== '' && { email }),
    ...(account !== undefined && { account }),
  };
}

export function AddSubscription({ onAdded, onClose }: { readonly onAdded: () => void; readonly onClose: () => void }) {
  const { t } = useI18n();
  // until the device is known the form is the single login of KOR-69; the new fields appear with a parallel device
  const parallel = useParallel() === true;
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [started, setStarted] = useState<LoginInput | null>(null);
  const [automatic, setAutomatic] = useState(false);
  const [email, setEmail] = useState('');
  const [mailbox, setMailbox] = useState<MailboxDraft>(EMPTY_MAILBOX);
  const [account, setAccount] = useState<AccountDraft>(EMPTY_ACCOUNT);
  const [orgs, setOrgs] = useState<readonly string[]>([]);
  const [id] = useState(() => newSubscriptionId());
  const input = addInput(id, { name, note, automatic, email, mailbox, account, orgs }, parallel);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setStarted(input);
  };
  if (started !== null) return <SubscriptionLogin input={started} onDone={onAdded} onClose={onClose} />;
  return (
    <form className="flex flex-col gap-3 rounded border border-gray-300 p-4" onSubmit={submit} data-testid="subadd">
      <label htmlFor="subadd-name">{t('subscription-login.name')}</label>
      <input
        id="subadd-name"
        className={INPUT}
        maxLength={64}
        value={name}
        onChange={(event) => setName(event.target.value)}
        data-testid="subadd.name"
      />
      <label htmlFor="subadd-note">{t('subscription-login.note')}</label>
      <input
        id="subadd-note"
        className={INPUT}
        maxLength={500}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        data-testid="subadd.note"
      />
      {!parallel && (
        <label className="flex min-h-11 items-center gap-2">
          <input
            type="checkbox"
            checked={automatic}
            onChange={() => setAutomatic(!automatic)}
            data-testid="subadd.automatic"
          />
          {t('subscription-login.automatic')}
        </label>
      )}
      {parallel && <MailboxFields id="subadd.mailbox" value={mailbox} onChange={setMailbox} />}
      {(automatic || parallel) && (
        <>
          <label htmlFor="subadd-email">
            {t(parallel ? 'subscription-login.sign-in-email' : 'subscription-login.email')}
          </label>
          <input
            id="subadd-email"
            type="email"
            className={INPUT}
            maxLength={254}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            data-testid="subadd.email"
          />
        </>
      )}
      {parallel && mailboxOf(mailbox) !== undefined && (
        <AccountFields id="subadd.account" value={account} onChange={setAccount} />
      )}
      {parallel && <OrgChoice id="subadd.orgs" value={orgs} onChange={setOrgs} />}
      <button type="submit" className={PRIMARY} disabled={input === null} data-testid="subadd.start">
        {t('subscription-login.add.start')}
      </button>
      <button type="button" className={BUTTON} onClick={onClose} data-testid="subadd.close">
        {t('subscription-login.close')}
      </button>
    </form>
  );
}
