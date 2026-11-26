// The release of a mailbox to workgroups (KOR-74, ADR-099 section 3; rule E-31): checkboxes of the active workgroups
// of the user's organizations where the user has explicit access, plus the workgroups it is released to already (so a
// release the user can no longer give can still be switched off). Saved sealed in the record and as the hub's mirror
// (`releaseMailbox`); nothing checked = released nowhere.
import { useMemo, useState } from 'react';
import { meOrgs } from '../../../../core/account/session';
import { type Mailbox, releasableWorkgroups, releaseMailbox } from '../../../../core/credentials/mailboxes';
import { requestErrorCode } from '../../../../core/requests/client';
import { orgEntry } from '../../../../core/store/slices/orgs';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useDirectory } from '../../../screens/tasks/hooks';
import { useAppEnv } from '../../../shell/context';
import { PRIMARY } from '../credentials/DevicePicker';
import { useReason } from '../github/reason';

/** The workgroups the user can release to, by workgroup ID → organization. */
export function useReleasable(): ReadonlyMap<string, string> {
  const { app } = useAppEnv();
  const profile = useStore((state) => state.me.profile, app.store);
  const userId = useStore((state) => state.me.userId, app.store);
  const orgs = useStore((state) => state.orgs, app.store);
  return useMemo(() => {
    const ids = meOrgs(profile).map((item) => item.org_id);
    return new Map(releasableWorkgroups(orgs, ids, userId).map((item) => [item.wg, item.org]));
  }, [profile, userId, orgs]);
}

/** A workgroup with its organization by their names (the start of the ID until the names are read). */
export function WorkgroupName({ org, wg }: { readonly org: string | undefined; readonly wg: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const known = org ?? '';
  const verified = useStore((state) => orgEntry(state.orgs, known).log !== null, app.store);
  const directory = useDirectory(known, verified);
  const workgroup = directory.workgroups.get(wg) ?? wg.slice(0, 8);
  return <>{t('mailboxes.workgroup', { workgroup, org: directory.orgName ?? known.slice(0, 8) })}</>;
}

export function Release({ item, onSaved }: { readonly item: Mailbox; readonly onSaved: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const releasable = useReleasable();
  const shown = [...new Set([...releasable.keys(), ...item.wgs])];
  const [chosen, setChosen] = useState<readonly string[]>(item.wgs);
  const [state, setState] = useState<{ readonly kind: 'idle' | 'saving' | 'saved' } | { kind: 'error'; code: string }>({
    kind: 'idle',
  });
  const toggle = (wg: string) =>
    setChosen((current) => (current.includes(wg) ? current.filter((other) => other !== wg) : [...current, wg]));
  const save = async () => {
    setState({ kind: 'saving' });
    try {
      await releaseMailbox(app.tasks, item.id, chosen);
      setState({ kind: 'saved' });
      onSaved();
    } catch (error) {
      setState({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  return (
    <fieldset className="flex flex-col gap-1" data-testid="mbrelease">
      <legend>{t('mailboxes.release.label')}</legend>
      {shown.length === 0 && <p data-testid="mbrelease.none">{t('mailboxes.release.none')}</p>}
      {shown.map((wg) => (
        <label key={wg} className="flex min-h-11 items-center gap-2">
          <input
            type="checkbox"
            checked={chosen.includes(wg)}
            onChange={() => toggle(wg)}
            data-testid={`mbrelease.wg.${wg}`}
          />
          <WorkgroupName org={releasable.get(wg)} wg={wg} />
        </label>
      ))}
      <button
        type="button"
        className={PRIMARY}
        disabled={state.kind === 'saving'}
        onClick={() => void save()}
        data-testid="mbrelease.save"
      >
        {t('mailboxes.release.save')}
      </button>
      {state.kind === 'saved' && (
        <p role="status" data-testid="mbrelease.saved">
          {t('mailboxes.saved')}
        </p>
      )}
      {state.kind === 'error' && (
        <p role="alert" data-testid="mbrelease.failed" data-code={state.code}>
          {t('mailboxes.save-failed', { reason: reason(state.code) })}
        </p>
      )}
    </fieldset>
  );
}
