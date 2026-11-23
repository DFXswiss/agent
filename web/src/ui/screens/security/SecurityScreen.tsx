// Security page (06-web.md section 10.5, anonymous): the build hash (SHA-256 of the delivered `build-manifest.json`),
// version and commit, the fingerprint of the compiled platform root key (grouped hex; a manifest naming another key is
// flagged), the hub key ID, and plain explanations: what the hub sees and cannot see (04-crypto.md section 7), that
// end-to-end encryption in the browser depends on the delivered JavaScript and how to compare the build hash with the
// published one (04-crypto.md section 9), and that execution devices process the plaintext of their workgroups.
import { useEffect, useState } from 'react';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { type BuildInfo, groupHex, loadBuild, rootKeyFingerprint } from './build';

const SEES = ['ids', 'logs', 'passkeys', 'headers', 'states', 'operations', 'metrics', 'notifications', 'audit'];
const HIDDEN = ['content', 'names', 'codes', 'keys', 'credentials'];

type Loaded<T> = { readonly status: 'loading' } | { readonly status: 'done'; readonly value: T };

function Value({ testId, children }: { readonly testId: string; readonly children: React.ReactNode }) {
  return (
    <dd className="break-all font-mono text-sm" data-testid={testId}>
      {children}
    </dd>
  );
}

function Explained({ id, items }: { readonly id: string; readonly items?: readonly string[] }) {
  const { t } = useI18n();
  return (
    <section className="flex flex-col gap-2" aria-labelledby={`security-${id}`} data-testid={`security.${id}`}>
      <h2 id={`security-${id}`} className="text-lg font-semibold">
        {t(`security.${id}.title`)}
      </h2>
      <p>{t(`security.${id}.body`)}</p>
      {items !== undefined && (
        <ul className="list-disc pl-6">
          {items.map((item) => (
            <li key={item}>{t(`security.${id}.${item}`)}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function SecurityScreen() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [build, setBuild] = useState<Loaded<BuildInfo | null>>({ status: 'loading' });
  const [fingerprint, setFingerprint] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    void loadBuild(app.fetch).then((value) => live && setBuild({ status: 'done', value }));
    void rootKeyFingerprint(app.config.platform_root_key).then((value) => live && setFingerprint(value));
    return () => {
      live = false;
    };
  }, [app]);
  const info = build.status === 'done' ? build.value : null;
  const pending = build.status === 'loading' ? t('security.loading') : t('security.unavailable');
  const mismatch = info !== null && fingerprint !== null && info.rootKeyFingerprint !== fingerprint;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4" data-testid="security.screen">
      <h1 className="text-2xl font-semibold">{t('security.title')}</h1>
      <p>{t('security.intro')}</p>
      <section className="flex flex-col gap-3" aria-labelledby="security-build" data-testid="security.build">
        <h2 id="security-build" className="text-lg font-semibold">
          {t('security.build.title')}
        </h2>
        <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-[max-content_1fr]">
          <dt className="font-medium">{t('security.build.hash')}</dt>
          <Value testId="security.build-hash">{info === null ? pending : groupHex(info.hash)}</Value>
          <dt className="font-medium">{t('security.build.version')}</dt>
          <Value testId="security.version">{info === null ? pending : info.version}</Value>
          <dt className="font-medium">{t('security.build.commit')}</dt>
          <Value testId="security.commit">{info === null ? pending : info.commit}</Value>
          <dt className="font-medium">{t('security.build.root-key')}</dt>
          <Value testId="security.root-key">
            {fingerprint === null ? t('security.loading') : groupHex(fingerprint)}
          </Value>
          <dt className="font-medium">{t('security.build.hub-key')}</dt>
          <Value testId="security.hub-key">{app.config.hub_key.id}</Value>
        </dl>
        {mismatch && (
          <p role="alert" className="font-semibold text-red-800" data-testid="security.root-key.mismatch">
            {t('security.build.mismatch')}
          </p>
        )}
        <p data-testid="security.compare">{t('security.build.compare')}</p>
      </section>
      <Explained id="sees" items={SEES} />
      <Explained id="hidden" items={HIDDEN} />
      <Explained id="javascript" />
      <Explained id="devices" />
    </main>
  );
}
