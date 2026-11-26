// One Claude subscription with its actions (KOR-69, ADR-090): status and address; "Log in now" for one that is not
// logged in (or "Log in again"), label and note, use, log out and remove. The primary subscription (own sessions) is
// shown only; it is changed in the credentials section. No token is ever shown.
import { type FormEvent, useState } from 'react';
import {
  logoutSubscription,
  noteSubscription,
  removeSubscription,
  renameSubscription,
  REVOKE_URL,
  type Subscription,
} from '../../core/credentials/subscriptions';
import { isLabel } from '../../core/requests/bodies';
import { requestErrorCode } from '../../core/requests/client';
import { useI18n } from '../i18n';
import { BUTTON, PRIMARY } from '../settings/sections/credentials/DevicePicker';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';
import { SubscriptionLogin } from './SubscriptionLogin';
import { UseEditor } from './UseEditor';

type Open = 'none' | 'login' | 'edit' | 'use' | 'remove';

export function SubscriptionCard({ item, onChanged }: { readonly item: Subscription; readonly onChanged: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [open, setOpen] = useState<Open>('none');
  const current = { name: item.name ?? '', note: item.note ?? '' };
  const [name, setName] = useState(current.name);
  const [note, setNote] = useState(current.note);
  const [failure, setFailure] = useState<string | null>(null);
  const [revoke, setRevoke] = useState(false);
  const run = async (work: () => Promise<void>, after: () => void = () => setOpen('none')) => {
    setFailure(null);
    try {
      await work();
      after();
      onChanged();
    } catch (error) {
      setFailure(requestErrorCode(error));
    }
  };
  const saveLabels = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      if (name.trim() !== current.name) await renameSubscription(app.tasks, item.id, name);
      if (note.trim() !== current.note) await noteSubscription(app.tasks, item.id, note);
    });
  };
  const title = item.primary ? t('subscription-login.primary') : (item.name ?? t('subscription-login.unnamed'));
  const changeable = !item.primary && item.status !== 'unverifiable';
  const loginClass = item.status === 'not_logged_in' ? PRIMARY : BUTTON;
  const removable = !item.primary && item.status === 'unverifiable' && open === 'none';
  return (
    <article className="flex flex-col gap-2 rounded border border-gray-300 p-3" data-testid={`subcard.${item.id}`}>
      <h4 className="font-semibold">{title}</h4>
      {item.email !== null && <p className="text-sm">{item.email}</p>}
      {item.note !== null && <p className="text-sm text-gray-700">{item.note}</p>}
      <p data-testid="subcard.status" data-status={item.status}>
        {t(`subscription-login.status.${item.status}`)}
      </p>
      {revoke && (
        <p role="status" data-testid="subcard.revoke">
          {t('subscription-login.revoke')}{' '}
          <a href={REVOKE_URL} target="_blank" rel="noopener noreferrer">
            {REVOKE_URL}
          </a>
        </p>
      )}
      {changeable && open === 'none' && (
        <div className="flex flex-wrap gap-2">
          <button type="button" className={loginClass} onClick={() => setOpen('login')} data-testid="subcard.login">
            {item.status === 'not_logged_in' ? t('subscription-login.login-now') : t('subscription-login.login-again')}
          </button>
          <button type="button" className={BUTTON} onClick={() => setOpen('edit')} data-testid="subcard.edit">
            {t('subscription-login.edit')}
          </button>
          <button type="button" className={BUTTON} onClick={() => setOpen('use')} data-testid="subcard.use">
            {t('subscription-login.use.open')}
          </button>
          {item.status !== 'not_logged_in' && (
            <button
              type="button"
              className={BUTTON}
              onClick={() =>
                void run(
                  () => logoutSubscription(app.tasks, item.id),
                  () => setRevoke(true),
                )
              }
              data-testid="subcard.logout"
            >
              {t('subscription-login.logout')}
            </button>
          )}
          <button type="button" className={BUTTON} onClick={() => setOpen('remove')} data-testid="subcard.remove">
            {t('subscription-login.remove')}
          </button>
        </div>
      )}
      {removable && (
        <button type="button" className={BUTTON} onClick={() => setOpen('remove')} data-testid="subcard.remove">
          {t('subscription-login.remove')}
        </button>
      )}
      {open === 'login' && (
        <SubscriptionLogin
          input={{ subscriptionId: item.id, mode: 'manual' }}
          onDone={() => {
            setOpen('none');
            onChanged();
          }}
          onClose={() => setOpen('none')}
        />
      )}
      {open === 'edit' && (
        <form className="flex flex-col gap-2" onSubmit={saveLabels} data-testid="subcard.labels">
          <label htmlFor={`subname-${item.id}`}>{t('subscription-login.name')}</label>
          <input
            id={`subname-${item.id}`}
            maxLength={64}
            className="min-h-11 rounded border border-gray-400 px-3"
            value={name}
            onChange={(event) => setName(event.target.value)}
            data-testid="subcard.name"
          />
          <label htmlFor={`subnote-${item.id}`}>{t('subscription-login.note')}</label>
          <input
            id={`subnote-${item.id}`}
            maxLength={500}
            className="min-h-11 rounded border border-gray-400 px-3"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            data-testid="subcard.note"
          />
          <button type="submit" className={PRIMARY} disabled={!isLabel(name.trim(), 64)} data-testid="subcard.save">
            {t('subscription-login.save')}
          </button>
          <button type="button" className={BUTTON} onClick={() => setOpen('none')} data-testid="subcard.cancel">
            {t('subscription-login.cancel')}
          </button>
        </form>
      )}
      {open === 'use' && item.use !== null && (
        <>
          <UseEditor id={item.id} use={item.use} onSaved={onChanged} />
          <button type="button" className={BUTTON} onClick={() => setOpen('none')} data-testid="subcard.use.close">
            {t('subscription-login.close')}
          </button>
        </>
      )}
      {open === 'remove' && (
        <div className="flex flex-col gap-2" role="alertdialog" data-testid="subcard.confirm">
          <p>{t('subscription-login.remove.confirm')}</p>
          <button
            type="button"
            className={PRIMARY}
            onClick={() => void run(() => removeSubscription(app.tasks, item.id))}
            data-testid="subcard.remove.yes"
          >
            {t('subscription-login.remove')}
          </button>
          <button type="button" className={BUTTON} onClick={() => setOpen('none')} data-testid="subcard.remove.no">
            {t('subscription-login.cancel')}
          </button>
        </div>
      )}
      {failure !== null && (
        <p role="alert" data-testid="subcard.failed" data-code={failure}>
          {t('subscription-login.save-failed', { reason: reason(failure) })}
        </p>
      )}
    </article>
  );
}
