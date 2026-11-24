// "This passkey cannot unlock your data" (06-web.md section 3.3; KOR-22, ADR-055): a passkey whose sign-in gave no PRF
// output is not accepted for a new account, as a further passkey or as the new passkey of a recovery. One message for
// all three, with what works and what does not (texts in the `passkeys` catalog, the one place of the provider list),
// the hint to delete the unusable passkey in the password manager, and "Try again with another passkey".
import { useI18n } from '../i18n';

export interface PrfRequiredProps {
  /** Where the passkey was refused (the line about what happened). */
  readonly context: 'register' | 'add' | 'recover';
  /** Registration only: false if the hub could not free the invitation at once (it does so after 24 h). */
  readonly released?: boolean;
  /** Adding and recovery: false if the refused passkey could not be removed from the account. */
  readonly removed?: boolean;
  readonly onRetry: () => void;
}

export function PrfRequired({ context, released = true, removed = true, onRetry }: PrfRequiredProps) {
  const { t } = useI18n();
  return (
    <section role="alert" className="flex flex-col gap-3" data-testid="prf-required" data-context={context}>
      <h2 className="text-xl font-semibold">{t('passkeys.prf-required.title')}</h2>
      <p className="font-semibold">{t('passkeys.prf-required.body')}</p>
      {released ? (
        <p>{t(`passkeys.prf-required.${context}`)}</p>
      ) : (
        <p data-testid="prf-required.later">{t('passkeys.prf-required.later')}</p>
      )}
      {!removed && <p data-testid="prf-required.not-removed">{t('passkeys.prf-required.not-removed')}</p>}
      <ul className="list-disc pl-6">
        <li>{t('passkeys.prf-required.works')}</li>
        <li>{t('passkeys.prf-required.bitwarden')}</li>
      </ul>
      <p className="text-sm text-gray-700">{t('passkeys.prf-required.delete')}</p>
      <button
        type="button"
        className="min-h-11 self-start rounded bg-gray-900 px-4 text-white"
        onClick={onRetry}
        data-testid="prf-required.retry"
      >
        {t('passkeys.prf-required.retry')}
      </button>
    </section>
  );
}
