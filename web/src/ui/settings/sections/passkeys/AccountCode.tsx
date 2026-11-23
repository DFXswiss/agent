// The user's own account code in `/settings` (ADR-060): computed by the browser from the unlocked identity (never taken
// from the hub), shown as `XXXX-XXXX-XXXX` with "Copy". An admin of another organization types it to admit the account
// directly; the admitting browser recomputes it from the proven keys, so the code also checks them.
import { useEffect, useState } from 'react';
import { formatAccountCode } from '../../../../core/crypto/identity';
import { ownAccountCode } from '../../../../core/org/admit';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';

const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4 disabled:opacity-60';

export function AccountCode() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const identity = useStore((state) => state.me.identity, app.store);
  const [code, setCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    ownAccountCode(app.tasks).then(
      (found) => setCode(found === null ? null : formatAccountCode(found)),
      () => setCode(null),
    );
  }, [app, identity]);
  if (code === null) return null;
  return (
    <div className="flex flex-col gap-2" data-testid="passkeys.account-code">
      <h3 className="text-lg font-semibold">{t('passkeys.account-code.title')}</h3>
      <p>{t('passkeys.account-code.explain')}</p>
      <p className="font-mono text-lg" data-testid="passkeys.account-code.value">
        {code}
      </p>
      <button
        type="button"
        className={BUTTON}
        onClick={() => {
          navigator.clipboard.writeText(code).then(
            () => setCopied(true),
            () => setCopied(false),
          );
        }}
        data-testid="passkeys.account-code.copy"
      >
        {copied ? t('passkeys.account-code.copied') : t('passkeys.account-code.copy')}
      </button>
    </div>
  );
}
