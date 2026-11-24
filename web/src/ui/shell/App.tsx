// Root of the signed-in client: the locale follows `user_settings.locale` once known (06-web.md section 2.5), else the
// browser's languages; the router renders the screens inside the shell layout.
import { useEffect, useMemo } from 'react';
import { RouterProvider } from 'react-router-dom';
import { createI18n, pickLocale } from '../../core/i18n';
import { useStore } from '../hooks/useStore';
import { I18nProvider } from '../i18n';
import { createAppRouter, PROVIDER_FUTURE } from '../router';
import { type AppEnv, AppEnvProvider } from './context';

export interface AppProps {
  readonly env: AppEnv;
  readonly languages: readonly string[];
  readonly router?: ReturnType<typeof createAppRouter>;
}

export function App({ env, languages, router }: AppProps) {
  const userLocale = useStore((state) => state.session.locale, env.app.store);
  const locale = pickLocale(userLocale, languages);
  const i18n = useMemo(() => createI18n(locale), [locale]);
  const appRouter = useMemo(() => router ?? createAppRouter(), [router]);
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);
  return (
    <AppEnvProvider value={env}>
      <I18nProvider value={i18n}>
        <RouterProvider router={appRouter} future={PROVIDER_FUTURE} />
      </I18nProvider>
    </AppEnvProvider>
  );
}
