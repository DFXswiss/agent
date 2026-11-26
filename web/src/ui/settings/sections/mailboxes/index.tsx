// Settings section "Mailboxes" (KOR-74, ADR-099 section 9): the entry of the settings registry. The section itself
// (`MailboxesView`) and its core module are loaded on demand when the settings show it, so the first load of the app
// stays within its budget (06-web.md section 12.2).
import { lazy, Suspense } from 'react';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useI18n } from '../../../i18n';
import { ChunkBoundary } from '../../../lazy/ChunkBoundary';

const MailboxesView = lazy(() => import('./MailboxesView'));

export function LazyMailboxesSection() {
  const { t } = useI18n();
  return (
    <ChunkBoundary fallback={<p data-testid="mailboxes.unavailable">{t('app.screen-unavailable.title')}</p>}>
      <Suspense fallback={<p data-testid="mailboxes.loading">{t('app.loading')}</p>}>
        <MailboxesView />
      </Suspense>
    </ChunkBoundary>
  );
}

export const section: SettingsSection = {
  id: 'mailboxes',
  order: 62,
  titleKey: 'mailboxes.title',
  Component: LazyMailboxesSection,
};
