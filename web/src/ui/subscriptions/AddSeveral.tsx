// "Add several subscriptions" (KOR-73, ADR-099 section 2): one row per subscription (label, address, its own mailbox,
// the data of a new account), one choice of the organizations that may use them, and one button that starts every
// login at once on a device that runs logins side by side (`addSubscriptions`, each in its own sandbox and slot). The
// progress of each subscription is shown in its row (`LoginRequest`: stages, the check for humans, link and code,
// done or failed); "again" restarts the login of that row alone (its record exists by then).
import { type FormEvent, useCallback, useState } from 'react';
import {
  type BatchEntry,
  type BatchStarted,
  addSubscriptions,
  startSubscriptionLogin,
  subscriptionDevices,
} from '../../core/credentials/subscriptions';
import { EMAIL_RE, isLabel } from '../../core/requests/bodies';
import { type PreparedRequest, requestErrorCode } from '../../core/requests/client';
import { useI18n } from '../i18n';
import { BUTTON, PRIMARY } from '../settings/sections/credentials/DevicePicker';
import { LoginRequest } from '../settings/sections/credentials/LoginFlow';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';
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
} from './fields';
import '../../core/i18n/lazy/subscription-login';

const INPUT = 'min-h-11 rounded border border-gray-400 px-3';

interface Row {
  readonly key: number;
  readonly name: string;
  readonly email: string;
  readonly mailbox: MailboxDraft;
  readonly account: AccountDraft;
}

const emptyRow = (key: number): Row => ({ key, name: '', email: '', mailbox: EMPTY_MAILBOX, account: EMPTY_ACCOUNT });

/** The batch entry of a row (`null` while it is not valid). */
export function entryOf(row: Row, orgs: readonly string[]): BatchEntry | null {
  const email = row.email.trim();
  if (!isLabel(row.name.trim(), 64) || !(email === '' || (EMAIL_RE.test(email) && email.length <= 254))) return null;
  const mailbox = mailboxOf(row.mailbox);
  const account = mailbox === undefined ? undefined : accountOf(row.account);
  if (mailbox === null || account === null) return null;
  return {
    record: { name: row.name, email, orgs, ...(mailbox !== undefined && { mailbox }) },
    ...(account !== undefined && { account }),
  };
}

function Started({
  device,
  entry,
  started,
  onDone,
}: {
  readonly device: string;
  readonly entry: BatchEntry;
  readonly started: BatchStarted;
  readonly onDone: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [prepared, setPrepared] = useState<PreparedRequest | null>('prepared' in started ? started.prepared : null);
  const [error, setError] = useState<string | null>('error' in started ? started.error : null);
  const [done, setDone] = useState(false);
  const own = entry.record.mailbox !== undefined;
  // stable: the login calls it from an effect each time it changes
  const finished = useCallback(() => {
    setDone(true);
    onDone();
  }, [onDone]);
  const again = async () => {
    setError(null);
    setPrepared(null);
    try {
      const input = {
        subscriptionId: started.subscriptionId,
        mode: own ? ('automatic' as const) : ('manual' as const),
        // the record again: written when the first attempt did not, left alone when it did
        create: entry.record,
        ...(entry.account !== undefined && { account: entry.account }),
      };
      setPrepared(await startSubscriptionLogin(app.tasks, device, input, true));
    } catch (failure) {
      setError(requestErrorCode(failure));
    }
  };
  return (
    <li
      className="flex flex-col gap-2 rounded border border-gray-300 p-3"
      data-testid={`subbatch.row.${started.subscriptionId}`}
    >
      <h5 className="font-semibold">{entry.record.name}</h5>
      {error !== null && (
        <>
          <p role="alert" data-testid="subbatch.start-failed" data-code={error}>
            {t('subscription-login.batch.start-failed', { reason: reason(error) })}
          </p>
          <button type="button" className={BUTTON} onClick={() => void again()} data-testid="subbatch.again">
            {t('subscription-login.retry')}
          </button>
        </>
      )}
      {prepared !== null && <LoginRequest prepared={prepared} onDone={finished} onRetry={() => void again()} />}
      {done && (
        <p role="status" data-testid="subbatch.done">
          {t('subscription-login.batch.done')}
        </p>
      )}
    </li>
  );
}

export function AddSeveral({ onAdded, onClose }: { readonly onAdded: () => void; readonly onClose: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [rows, setRows] = useState<readonly Row[]>([emptyRow(1), emptyRow(2)]);
  const [orgs, setOrgs] = useState<readonly string[]>([]);
  const [phase, setPhase] = useState<
    | { readonly kind: 'form' }
    | { readonly kind: 'starting' }
    | { readonly kind: 'none' }
    | { readonly kind: 'error'; readonly code: string }
    | {
        readonly kind: 'started';
        readonly device: string;
        readonly entries: readonly BatchEntry[];
        readonly started: readonly BatchStarted[];
      }
  >({ kind: 'form' });
  const entries = rows.map((row) => entryOf(row, orgs));
  const valid = entries.every((entry) => entry !== null);
  const change = (key: number, next: Partial<Row>) =>
    setRows(rows.map((row) => (row.key === key ? { ...row, ...next } : row)));
  const start = async (event: FormEvent) => {
    event.preventDefault();
    const ready = entries.filter((entry): entry is BatchEntry => entry !== null);
    setPhase({ kind: 'starting' });
    try {
      const found = await subscriptionDevices(app.tasks);
      if (found.preselected === null || !found.parallel) return setPhase({ kind: 'none' });
      const started = await addSubscriptions(app.tasks, found.preselected, ready);
      setPhase({ kind: 'started', device: found.preselected, entries: ready, started });
    } catch (error) {
      setPhase({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid="subbatch">
      <h4 className="font-semibold">{t('subscription-login.batch')}</h4>
      <p>{t('subscription-login.batch.intro')}</p>
      {phase.kind === 'form' && (
        <form className="flex flex-col gap-3" onSubmit={(event) => void start(event)} data-testid="subbatch.form">
          {rows.map((row, index) => (
            <fieldset
              key={row.key}
              className="flex flex-col gap-1 rounded border border-gray-200 p-3"
              data-testid={`subbatch.entry.${index}`}
            >
              <legend>{t('subscription-login.batch.row', { number: index + 1 })}</legend>
              <label htmlFor={`subbatch-${row.key}-name`}>{t('subscription-login.name')}</label>
              <input
                id={`subbatch-${row.key}-name`}
                className={INPUT}
                maxLength={64}
                value={row.name}
                onChange={(event) => change(row.key, { name: event.target.value })}
                data-testid={`subbatch.entry.${index}.name`}
              />
              <label htmlFor={`subbatch-${row.key}-email`}>{t('subscription-login.sign-in-email')}</label>
              <input
                id={`subbatch-${row.key}-email`}
                type="email"
                className={INPUT}
                maxLength={254}
                value={row.email}
                onChange={(event) => change(row.key, { email: event.target.value })}
                data-testid={`subbatch.entry.${index}.email`}
              />
              <MailboxFields
                id={`subbatch.entry.${index}.mailbox`}
                value={row.mailbox}
                onChange={(mailbox) => change(row.key, { mailbox })}
              />
              {mailboxOf(row.mailbox) !== undefined && (
                <AccountFields
                  id={`subbatch.entry.${index}.account`}
                  value={row.account}
                  onChange={(account) => change(row.key, { account })}
                />
              )}
              {rows.length > 1 && (
                <button
                  type="button"
                  className={BUTTON}
                  onClick={() => setRows(rows.filter((item) => item.key !== row.key))}
                  data-testid={`subbatch.entry.${index}.remove`}
                >
                  {t('subscription-login.batch.remove-row')}
                </button>
              )}
            </fieldset>
          ))}
          <button
            type="button"
            className={BUTTON}
            onClick={() => setRows([...rows, emptyRow(Math.max(...rows.map((row) => row.key)) + 1)])}
            data-testid="subbatch.add-row"
          >
            {t('subscription-login.batch.add-row')}
          </button>
          <OrgChoice id="subbatch.orgs" value={orgs} onChange={setOrgs} />
          <button type="submit" className={PRIMARY} disabled={!valid} data-testid="subbatch.start">
            {t('subscription-login.batch.start')}
          </button>
        </form>
      )}
      {phase.kind === 'starting' && (
        <p aria-live="polite" data-testid="subbatch.starting">
          {t('subscription-login.starting')}
        </p>
      )}
      {phase.kind === 'none' && (
        <p role="alert" data-testid="subbatch.no-device">
          {t('subscription-login.batch.no-device')}
        </p>
      )}
      {phase.kind === 'error' && (
        <p role="alert" data-testid="subbatch.failed" data-code={phase.code}>
          {t('subscription-login.failed', { reason: reason(phase.code) })}
        </p>
      )}
      {phase.kind === 'started' && (
        <>
          <h5 className="font-semibold">{t('subscription-login.batch.progress')}</h5>
          <ul className="flex flex-col gap-2" data-testid="subbatch.progress">
            {phase.started.map((started, index) => (
              <Started
                key={started.subscriptionId}
                device={phase.device}
                entry={phase.entries[index] as BatchEntry}
                started={started}
                onDone={onAdded}
              />
            ))}
          </ul>
        </>
      )}
      <button type="button" className={BUTTON} onClick={onClose} data-testid="subbatch.close">
        {t('subscription-login.close')}
      </button>
    </section>
  );
}
