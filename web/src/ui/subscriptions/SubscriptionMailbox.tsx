// The own mailbox of a further subscription (KOR-73, ADR-098 section 1): set or replace it (the password is entered
// anew, it is never shown), remove it, and check it on a device that runs logins side by side
// (`mailbox.check {subscription_id}`: IMAP login and LIST with the subscription's own mailbox).
import { useState } from 'react';
import { checkProgress, startCheck } from '../../core/credentials/login';
import { type Mailbox, setSubscriptionMailbox, subscriptionDevices } from '../../core/credentials/subscriptions';
import { type PreparedRequest, requestErrorCode } from '../../core/requests/client';
import { useI18n } from '../i18n';
import { BUTTON, PRIMARY } from '../settings/sections/credentials/DevicePicker';
import { useRequestView } from '../settings/sections/credentials/useRequestView';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';
import { EMPTY_MAILBOX, MailboxFields, type MailboxDraft, mailboxOf } from './fields';
import '../../core/i18n/lazy/subscription-login';

function CheckOutcome({ prepared }: { readonly prepared: PreparedRequest }) {
  const { t } = useI18n();
  const reason = useReason();
  const { view } = useRequestView(prepared);
  const progress = view === null ? { step: 'waiting' as const } : checkProgress(view);
  if (progress.step === 'waiting')
    return (
      <p aria-live="polite" data-testid="submailbox.checking">
        {t('subscription-login.mailbox.checking')}
      </p>
    );
  if (progress.step === 'failed')
    return (
      <p role="alert" data-testid="submailbox.check-failed" data-code={progress.code}>
        {t('subscription-login.mailbox.check-failed', { reason: reason(progress.code) })}
      </p>
    );
  return (
    <p role="status" data-testid="submailbox.checked" data-valid={String(progress.valid)}>
      {t(progress.valid ? 'subscription-login.mailbox.valid' : 'subscription-login.mailbox.invalid')}
    </p>
  );
}

export function SubscriptionMailbox({
  id,
  email,
  onChanged,
  onClose,
}: {
  readonly id: string;
  /** The address of the current own mailbox, null without one. */
  readonly email: string | null;
  readonly onChanged: () => void;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [draft, setDraft] = useState<MailboxDraft>(EMPTY_MAILBOX);
  const [failure, setFailure] = useState<string | null>(null);
  const [check, setCheck] = useState<PreparedRequest | null>(null);
  const mailbox = mailboxOf(draft);
  const write = async (value: Parameters<typeof setSubscriptionMailbox>[2]) => {
    setFailure(null);
    try {
      await setSubscriptionMailbox(app.tasks, id, value);
      onChanged();
      onClose();
    } catch (error) {
      setFailure(requestErrorCode(error));
    }
  };
  const runCheck = async () => {
    setFailure(null);
    setCheck(null);
    try {
      const found = await subscriptionDevices(app.tasks);
      if (found.preselected === null || !found.parallel) return setFailure('no_device_available');
      setCheck(await startCheck(app.tasks, found.preselected, 'mailbox', id));
    } catch (error) {
      setFailure(requestErrorCode(error));
    }
  };
  return (
    <section className="flex flex-col gap-2" data-testid="submailbox">
      {email !== null && <p data-testid="submailbox.current">{t('subscription-login.mailbox.of', { email })}</p>}
      <MailboxFields id="submailbox.fields" value={draft} onChange={setDraft} />
      <button
        type="button"
        className={PRIMARY}
        disabled={mailbox === null || mailbox === undefined}
        onClick={() => void write(mailbox as Mailbox)}
        data-testid="submailbox.save"
      >
        {t('subscription-login.mailbox.save')}
      </button>
      {email !== null && (
        <>
          <button type="button" className={BUTTON} onClick={() => void runCheck()} data-testid="submailbox.check">
            {t('subscription-login.mailbox.check')}
          </button>
          <button type="button" className={BUTTON} onClick={() => void write(null)} data-testid="submailbox.remove">
            {t('subscription-login.mailbox.remove')}
          </button>
        </>
      )}
      {check !== null && <CheckOutcome prepared={check} />}
      {failure !== null && (
        <p role="alert" data-testid="submailbox.failed" data-code={failure}>
          {t('subscription-login.save-failed', { reason: reason(failure) })}
        </p>
      )}
      <button type="button" className={BUTTON} onClick={onClose} data-testid="submailbox.close">
        {t('subscription-login.close')}
      </button>
    </section>
  );
}
