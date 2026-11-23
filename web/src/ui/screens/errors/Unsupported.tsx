// Unsupported-browser page (06-web.md section 12.6), shown instead of the app: what is missing (passkeys, X25519 or
// Ed25519 in WebCrypto, streaming fetch) and the list of supported browsers.
import { useI18n } from '../../i18n';
import type { Feature } from '../../shell/support';
import { ErrorLayout } from './ErrorLayout';

export const SUPPORTED = ['chrome', 'edge', 'safari', 'firefox'] as const;

export function Unsupported({ missing }: { readonly missing: readonly Feature[] }) {
  const { t } = useI18n();
  return (
    <ErrorLayout
      testId="shell.unsupported"
      prefix="unsupported"
      size="page"
      title={t('shell.unsupported.title')}
      body={
        <div className="flex flex-col gap-3">
          <p>{t('shell.unsupported.body')}</p>
          {missing.length > 0 && (
            <p data-testid="unsupported.missing">
              {t('error-pages.unsupported.missing', {
                features: missing.map((feature) => t(`error-pages.feature.${feature}`)).join(', '),
              })}
            </p>
          )}
          <h2 className="font-semibold">{t('error-pages.unsupported.supported')}</h2>
          <ul className="list-disc pl-6" data-testid="unsupported.browsers">
            {SUPPORTED.map((browser) => (
              <li key={browser}>{t(`error-pages.browser.${browser}`)}</li>
            ))}
          </ul>
        </div>
      }
    />
  );
}
