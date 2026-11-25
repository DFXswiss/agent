// "Add a subscription" (KOR-69, ADR-090): a label (and an optional note), one button – the login on a platform device
// starts at once (`SubscriptionLogin`) and the new subscription is only measured until its use is switched on.
import { type FormEvent, useState } from 'react';
import { newSubscriptionId } from '../../core/credentials/subscriptions';
import { EMAIL_RE, isLabel } from '../../core/requests/bodies';
import { useI18n } from '../i18n';
import { BUTTON, PRIMARY } from '../settings/sections/credentials/DevicePicker';
import { SubscriptionLogin } from './SubscriptionLogin';

export function AddSubscription({ onAdded, onClose }: { readonly onAdded: () => void; readonly onClose: () => void }) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [started, setStarted] = useState(false);
  const [automatic, setAutomatic] = useState(false);
  const [email, setEmail] = useState('');
  const [id] = useState(() => newSubscriptionId());
  const mailOk = !automatic || email.trim() === '' || (EMAIL_RE.test(email.trim()) && email.trim().length <= 254);
  const valid = isLabel(name.trim(), 64) && (note.trim() === '' || isLabel(note.trim(), 500)) && mailOk;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setStarted(true);
  };
  if (started) {
    const input = automatic
      ? { subscriptionId: id, mode: 'automatic' as const, name, note, email }
      : { subscriptionId: id, mode: 'manual' as const, name, note };
    return <SubscriptionLogin input={input} onDone={onAdded} onClose={onClose} />;
  }
  return (
    <form className="flex flex-col gap-3 rounded border border-gray-300 p-4" onSubmit={submit} data-testid="subadd">
      <label htmlFor="subadd-name">{t('subscription-login.name')}</label>
      <input
        id="subadd-name"
        className="min-h-11 rounded border border-gray-400 px-3"
        maxLength={64}
        value={name}
        onChange={(event) => setName(event.target.value)}
        data-testid="subadd.name"
      />
      <label htmlFor="subadd-note">{t('subscription-login.note')}</label>
      <input
        id="subadd-note"
        className="min-h-11 rounded border border-gray-400 px-3"
        maxLength={500}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        data-testid="subadd.note"
      />
      <label className="flex min-h-11 items-center gap-2">
        <input
          type="checkbox"
          checked={automatic}
          onChange={() => setAutomatic(!automatic)}
          data-testid="subadd.automatic"
        />
        {t('subscription-login.automatic')}
      </label>
      {automatic && (
        <>
          <label htmlFor="subadd-email">{t('subscription-login.email')}</label>
          <input
            id="subadd-email"
            type="email"
            className="min-h-11 rounded border border-gray-400 px-3"
            maxLength={254}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            data-testid="subadd.email"
          />
        </>
      )}
      <button type="submit" className={PRIMARY} disabled={!valid} data-testid="subadd.start">
        {t('subscription-login.add.start')}
      </button>
      <button type="button" className={BUTTON} onClick={onClose} data-testid="subadd.close">
        {t('subscription-login.close')}
      </button>
    </form>
  );
}
