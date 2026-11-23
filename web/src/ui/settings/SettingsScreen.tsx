// `/settings` (06-web.md sections 2.4, 3.6, 8): the frame with the discovered sections
// (`settings/sections/<area>/index.tsx`, ADR-027 section 4: passkeys, recovery code, model access, credentials, GitHub,
// quotas come from later packages) and "Sign out" (`POST /v1/session/end`, IndexedDB session entries deleted, memory
// cleared, section 2.6).
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { settingsSections, type SettingsSection } from '../registries/settings-sections';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

export function SettingsScreen({ sections = settingsSections() }: { readonly sections?: readonly SettingsSection[] }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const navigate = useNavigate();
  const [leaving, setLeaving] = useState(false);
  const signOut = async () => {
    setLeaving(true);
    await app.signOut();
    navigate('/login', { replace: true });
  };
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 p-6" data-testid="settings.page">
      <h1 className="text-2xl font-semibold">{t('settings.title')}</h1>
      {app.landingOrg() === null && <p data-testid="settings.no-org">{t('settings.no-org')}</p>}
      {sections.map(({ id, titleKey, Component }) => (
        <section
          key={id}
          className="flex flex-col gap-3"
          aria-labelledby={`settings-${id}`}
          data-testid={`settings.${id}`}
        >
          <h2 id={`settings-${id}`} className="text-xl font-semibold">
            {t(titleKey)}
          </h2>
          <Component />
        </section>
      ))}
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={() => void signOut()}
        disabled={leaving}
        data-testid="settings.sign-out"
      >
        {t('settings.sign-out')}
      </button>
    </main>
  );
}
