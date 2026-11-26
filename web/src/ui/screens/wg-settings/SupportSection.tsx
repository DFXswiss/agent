// Section "DFX support" of the workgroup settings (ADR-096; KOR-56), only for users who may release the workgroup
// (`OrgState.canRelease`: admins and writers): whether the workgroup is released (read or write, since when – the
// release itself from the verified log, the time from `GET /v1/orgs/{org}/support`), the current support persons by
// name (the roster of the verified platform log, named by the operator's names and the ID mark), and the actions
// release for reading, release for reading and writing, change read ↔ write and revoke – each after the confirmation
// dialog (`SupportDialog.tsx`; both load with the lazily loaded screens, not with the first load – a lazy import inside
// the screens chunk would be refused by the integrity loader, build/chunks.ts). An archived workgroup cannot be released (only revoked).
import '../../../core/i18n/lazy/support';
import { useEffect, useId, useState } from 'react';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import { actionErrorCode } from '../../../core/org/batch';
import type { Directory } from '../../../core/tasks/directory';
import type { OrgState } from '../../../core/log/state';
import { ensureMarks, rosterOf } from '../../../core/support/names';
import { loadOrgSupport } from '../../../core/support/people';
import { grantRelease, releaseOf, revokeRelease } from '../../../core/support/release';
import type { Access } from '../../../core/store/slices/support';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { supportText } from '../../names/support';
import { useAppEnv } from '../../shell/context';
import { BUTTON } from '../members/ActionDialog';
import { type SupportChange, SupportDialog } from './SupportDialog';

type Status =
  | { readonly kind: 'idle' }
  | { readonly kind: 'saving' }
  | { readonly kind: 'done'; readonly revoked: boolean }
  | { readonly kind: 'failed'; readonly code: string };

/** The current support persons as one line ("none" when the roster is empty). */
function usePeople(org: string): string {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const roster = useStore((state) => rosterOf(state.orgs.platform?.state ?? null), app.store);
  const entry = useStore((state) => state.support.orgs[org] ?? null, app.store);
  const marks = useStore((state) => state.support.marks, app.store);
  useEffect(() => {
    void ensureMarks(app.store, roster.keys());
  }, [app, roster]);
  if (roster.size === 0) return t('support.section.people.none');
  // the names come with the first answer; a later reload keeps them
  if (entry === null || (entry.status === 'loading' && Object.keys(entry.people).length === 0))
    return t('support.section.people.loading');
  const names = [...roster.keys()].map((id) =>
    supportText(t, { name: entry.people[id] ?? null, mark: marks[id] ?? null }),
  );
  return t('support.section.people', { names: names.join(', ') });
}

export function SupportSection({
  org,
  wg,
  state,
  directory,
}: {
  readonly org: string;
  readonly wg: string;
  readonly state: OrgState;
  readonly directory: Directory;
}) {
  const titleId = useId();
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const people = usePeople(org);
  const granted = useStore(
    (s) => s.support.orgs[org]?.releases.find((release) => release.wg_id === wg)?.granted_at ?? null,
    app.store,
  );
  const [open, setOpen] = useState<SupportChange | null>(null);
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  useEffect(() => {
    void loadOrgSupport(app.tasks, org);
  }, [app, org]);
  const release = releaseOf(state, wg);
  const writable = state.writable(wg);
  const confirm = async (change: SupportChange) => {
    setOpen(null);
    setStatus({ kind: 'saving' });
    try {
      if (change.kind === 'revoke') await revokeRelease(app.tasks, org, wg);
      else await grantRelease(app.tasks, org, wg, change.access, directory);
      setStatus({ kind: 'done', revoked: change.kind === 'revoke' });
    } catch (caught) {
      setStatus({ kind: 'failed', code: actionErrorCode(caught) });
    }
    await loadOrgSupport(app.tasks, org);
  };
  const ask = (change: SupportChange) => {
    setStatus({ kind: 'idle' });
    setOpen(change);
  };
  const since =
    granted === null
      ? null
      : formatEventTime(Date.parse(granted), { locale, timeZone: localTimeZone(), now: app.tasks.now() });
  const busy = status.kind === 'saving';
  const action = (testId: string, label: string, change: SupportChange) => (
    <button type="button" className={BUTTON} disabled={busy} onClick={() => ask(change)} data-testid={testId}>
      {t(label)}
    </button>
  );
  const other: Access = release === 'read' ? 'write' : 'read';
  return (
    <section className="flex flex-col gap-2" data-testid="support.section" aria-labelledby={titleId}>
      <h2 id={titleId} className="font-medium">
        {t('support.section.title')}
      </h2>
      <p className="text-sm text-gray-700">{t('support.section.intro')}</p>
      <p data-testid="support.section.state" data-release={release ?? 'none'}>
        {release === null
          ? t('support.section.none')
          : since === null
            ? t(`support.section.${release}.now`)
            : t(`support.section.${release}`, { since })}
      </p>
      <p className="text-sm" data-testid="support.section.people">
        {people}
      </p>
      {!writable && release === null && (
        <p className="text-sm text-gray-700" data-testid="support.section.archived">
          {t('support.section.archived')}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {release === null &&
          writable &&
          action('support.section.read', 'support.action.read', { kind: 'grant', access: 'read' })}
        {release === null &&
          writable &&
          action('support.section.write', 'support.action.write', { kind: 'grant', access: 'write' })}
        {release !== null &&
          writable &&
          action(`support.section.to-${other}`, `support.action.to-${other}`, { kind: 'change', access: other })}
        {release !== null && action('support.section.revoke', 'support.action.revoke', { kind: 'revoke' })}
      </div>
      {status.kind === 'saving' && (
        <p role="status" data-testid="support.section.saving">
          {t('support.saving')}
        </p>
      )}
      {status.kind === 'done' && (
        <p role="status" data-testid="support.section.done">
          {t(status.revoked ? 'support.done.revoke' : 'support.done.grant')}
        </p>
      )}
      {status.kind === 'failed' && (
        <p role="alert" className="text-red-800" data-testid="support.section.failed">
          {t('support.failed', { reason: error(status.code).title })}
        </p>
      )}
      {open !== null && (
        <SupportDialog
          change={open}
          people={people}
          onConfirm={() => void confirm(open)}
          onCancel={() => setOpen(null)}
        />
      )}
    </section>
  );
}
