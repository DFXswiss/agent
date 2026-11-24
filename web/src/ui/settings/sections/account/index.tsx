// Settings section "Account" (14-community.md section 6): the user ID with a copy button – the operator needs it to
// grant the community moderator role (`agent-hub community moderator grant --user ID`). The ID is not secret.
import { useState } from 'react';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';

type Copy = 'idle' | 'copied' | 'failed';

export function AccountSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const userId = useStore((state) => state.me.userId, app.store);
  const [copy, setCopy] = useState<Copy>('idle');
  if (userId === null) return null;
  const write = async () => {
    try {
      await navigator.clipboard.writeText(userId);
      setCopy('copied');
    } catch {
      setCopy('failed');
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <p className="font-medium">{t('account.user-id')}</p>
      <div className="flex flex-wrap items-center gap-3">
        <code
          className="select-all break-all rounded bg-gray-100 px-2 py-1 font-mono text-sm"
          data-testid="settings.account.user-id"
        >
          {userId}
        </code>
        <button
          type="button"
          className="min-h-11 rounded border border-gray-400 px-4"
          onClick={() => void write()}
          data-testid="settings.account.copy"
        >
          {t('account.copy')}
        </button>
      </div>
      <p className="text-sm text-gray-700">{t('account.user-id.help')}</p>
      {copy !== 'idle' && (
        <p role="status" data-testid={`settings.account.${copy}`}>
          {t(copy === 'copied' ? 'account.copied' : 'account.copy-failed')}
        </p>
      )}
    </div>
  );
}

export const section: SettingsSection = {
  id: 'account',
  order: 5,
  titleKey: 'account.title',
  Component: AccountSection,
};
