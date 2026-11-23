// Settings section "Language" (06-web.md section 2.5): the interface language, saved in `user_settings.locale`
// (`PUT /v1/me/settings`), so it follows the user to other browsers.
import { type ChangeEvent, useState } from 'react';
import { LOCALES, type Locale } from '../../../../core/i18n';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';

type Status = 'idle' | 'saving' | 'saved' | { readonly code: string };

export function LanguageSection() {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const [status, setStatus] = useState<Status>('idle');
  const change = async (event: ChangeEvent<HTMLSelectElement>) => {
    // the options are exactly LOCALES
    const next = event.target.value as Locale;
    setStatus('saving');
    try {
      await app.saveLocale(next);
      setStatus('saved');
    } catch (caught) {
      setStatus({ code: errorCode(caught) });
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor="settings-language">{t('settings.language.label')}</label>
      <select
        id="settings-language"
        className="min-h-11 self-start rounded border border-gray-400 px-3"
        value={locale}
        onChange={(event) => void change(event)}
        disabled={status === 'saving'}
        data-testid="settings.language.select"
      >
        {LOCALES.map((option) => (
          <option key={option} value={option}>
            {t(`settings.language.${option}`)}
          </option>
        ))}
      </select>
      {status === 'saved' && (
        <p role="status" data-testid="settings.language.saved">
          {t('settings.language.saved')}
        </p>
      )}
      {typeof status === 'object' && (
        <p role="alert" data-testid="settings.language.error">
          {error(status.code).title}
        </p>
      )}
    </div>
  );
}

export const section: SettingsSection = {
  id: 'language',
  order: 90,
  titleKey: 'settings.language.title',
  Component: LanguageSection,
};
