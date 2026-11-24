// Workgroup settings (06-web.md sections 2.4 and 8.1, route `/o/:org/w/:wg/settings`, right `wg:read`, edit
// `wg:write`): the repository allow-list (`owner/name`, the regex of 03-api.md section 9), the protected branch
// patterns (defaults prefilled) and the PR draft default, saved as a `settings.workgroup` event (the newest wins).
//
// The form starts from the newest verified settings event and follows newer ones while it is unchanged. Once the
// user edited it, a newer event shows the concurrent-edit warning: show the newer settings, or save the own ones
// anyway. Readers see the values without any control (06-web.md section 1). A workgroup the user cannot read is "not
// found" (existence is not revealed). Below the form: the workgroup's default GitHub identity (`GithubDefault.tsx`,
// ADR-078), editable by the organization's admins and the writers of the workgroup.
import { type FormEvent, useEffect, useId, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { WG } from '../../../core/log/state';
import { pullOnce } from '../../../core/tasks/pull';
import {
  type FieldError,
  KIND,
  parseSettings,
  saveSettings,
  selectCurrent,
  seqOf,
  SettingsError,
  type SettingsInput,
  settingsOf,
  toInput,
  type WorkgroupSettings,
} from '../../../core/wg-settings/settings';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { GithubDefault } from './GithubDefault';

interface Draft {
  readonly input: SettingsInput;
  /** The `scope_seq` of the settings the draft started from (or that saving it produced). */
  readonly from: number;
  readonly edited: boolean;
}

type Status =
  | { readonly kind: 'idle' }
  | { readonly kind: 'saving' }
  | { readonly kind: 'saved' }
  | { readonly kind: 'invalid'; readonly errors: readonly FieldError[] }
  | { readonly kind: 'conflict' }
  | { readonly kind: 'failed'; readonly code: string };

const AREA = 'min-h-24 rounded border border-gray-400 p-2 font-mono text-sm';
const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4';

function ReadOnly({ settings }: { readonly settings: WorkgroupSettings }) {
  const { t } = useI18n();
  return (
    <dl className="flex flex-col gap-3" data-testid="wg-settings.view">
      <dt className="font-medium">{t('wg-settings.repositories.label')}</dt>
      <dd data-testid="wg-settings.view.repositories">
        {settings.repositories.length === 0 ? (
          t('wg-settings.repositories.none')
        ) : (
          <ul className="font-mono text-sm">
            {settings.repositories.map((repo) => (
              <li key={repo}>{repo}</li>
            ))}
          </ul>
        )}
      </dd>
      <dt className="font-medium">{t('wg-settings.protected.label')}</dt>
      <dd data-testid="wg-settings.view.protected">
        <ul className="font-mono text-sm">
          {settings.protected_branches.map((pattern) => (
            <li key={pattern}>{pattern}</li>
          ))}
        </ul>
      </dd>
      <dd data-testid="wg-settings.view.draft">
        {settings.pr_draft_default ? t('wg-settings.draft.yes') : t('wg-settings.draft.no')}
      </dd>
    </dl>
  );
}

function Form({ org, wg }: { readonly org: string; readonly wg: string }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const ids = { repos: useId(), patterns: useId(), draft: useId() };
  const current = useStore(
    useMemo(() => selectCurrent(wg), [wg]),
    app.store,
  );
  // the form's own values once edited or saved; without them (or once a newer event than an unedited draft
  // arrived) the form shows the newest settings – an edited draft warns instead (module comment)
  const [draft, setDraft] = useState<Draft | null>(null);
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const latest = seqOf(current);
  const own = draft !== null && (draft.edited || draft.from >= latest);
  const input = own ? draft.input : toInput(settingsOf(current));
  const from = own ? draft.from : latest;
  const edit = (next: Partial<SettingsInput>) => {
    setDraft({ input: { ...input, ...next }, from, edited: true });
    if (status.kind === 'saved') setStatus({ kind: 'idle' });
  };
  const showNewer = () => {
    setDraft(null);
    setStatus({ kind: 'idle' });
  };
  const save = async (overwrite: boolean) => {
    const parsed = parseSettings(input);
    if (!parsed.ok) {
      setStatus({ kind: 'invalid', errors: parsed.errors });
      return;
    }
    setStatus({ kind: 'saving' });
    try {
      const seq = await saveSettings(app.tasks, { org, wg, settings: parsed.settings, base: from, overwrite });
      setDraft({ input: toInput(parsed.settings), from: seq, edited: false });
      setStatus({ kind: 'saved' });
    } catch (caught) {
      if (caught instanceof SettingsError && caught.code === 'settings_conflict') setStatus({ kind: 'conflict' });
      else setStatus({ kind: 'failed', code: caught instanceof SettingsError ? caught.code : errorCode(caught) });
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void save(false);
  };
  const conflict = status.kind === 'conflict' || (draft?.edited === true && latest > draft.from);
  return (
    <form className="flex flex-col gap-3" onSubmit={submit} data-testid="wg-settings.form" noValidate>
      <label htmlFor={ids.repos} className="font-medium">
        {t('wg-settings.repositories.label')}
      </label>
      <textarea
        id={ids.repos}
        className={AREA}
        value={input.repositories}
        onChange={(event) => edit({ repositories: event.target.value })}
        spellCheck={false}
        data-testid="wg-settings.repositories"
      />
      <p className="text-sm text-gray-600">{t('wg-settings.repositories.hint')}</p>
      <label htmlFor={ids.patterns} className="font-medium">
        {t('wg-settings.protected.label')}
      </label>
      <textarea
        id={ids.patterns}
        className={AREA}
        value={input.protectedBranches}
        onChange={(event) => edit({ protectedBranches: event.target.value })}
        spellCheck={false}
        data-testid="wg-settings.protected"
      />
      <p className="text-sm text-gray-600">{t('wg-settings.protected.hint')}</p>
      <div className="flex min-h-11 items-center gap-2">
        <input
          id={ids.draft}
          type="checkbox"
          className="h-5 w-5"
          checked={input.prDraftDefault}
          onChange={(event) => edit({ prDraftDefault: event.target.checked })}
          data-testid="wg-settings.draft"
        />
        <label htmlFor={ids.draft} className="flex min-h-11 items-center">
          {t('wg-settings.draft.label')}
        </label>
      </div>
      {status.kind === 'invalid' && (
        <ul role="alert" className="text-red-800" data-testid="wg-settings.errors">
          {status.errors.map((item) => (
            <li key={`${item.field} ${item.line} ${item.code}`} data-testid="wg-settings.error" data-field={item.field}>
              {t(`wg-settings.error.${item.code}`, { line: item.line, value: item.value })}
            </li>
          ))}
        </ul>
      )}
      {conflict && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded border border-amber-400 p-3"
          data-testid="wg-settings.conflict"
        >
          <p>{t('wg-settings.conflict')}</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={BUTTON} onClick={showNewer} data-testid="wg-settings.conflict.reload">
              {t('wg-settings.conflict.reload')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => void save(true)}
              data-testid="wg-settings.conflict.overwrite"
            >
              {t('wg-settings.conflict.overwrite')}
            </button>
          </div>
        </div>
      )}
      {status.kind === 'failed' && (
        <p role="alert" className="text-red-800" data-testid="wg-settings.failed">
          {t('wg-settings.failed', { reason: error(status.code).title })}
        </p>
      )}
      {status.kind === 'saved' && (
        <p role="status" data-testid="wg-settings.saved">
          {t('wg-settings.saved')}
        </p>
      )}
      <button
        type="submit"
        className="min-h-11 self-start rounded bg-blue-700 px-4 text-white"
        disabled={status.kind === 'saving'}
        data-testid="wg-settings.save"
      >
        {status.kind === 'saving' ? t('wg-settings.saving') : t('wg-settings.save')}
      </button>
    </form>
  );
}

function Settings({ org, wg, writer }: { readonly org: string; readonly wg: string; readonly writer: boolean }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const [pulled, setPulled] = useState<'loading' | 'ready' | { readonly code: string }>('loading');
  const current = useStore(
    useMemo(() => selectCurrent(wg), [wg]),
    app.store,
  );
  useEffect(() => {
    let live = true;
    pullOnce(app.tasks, org, wg, { kinds: [KIND] }).then(
      () => live && setPulled('ready'),
      (caught: unknown) => live && setPulled({ code: errorCode(caught) }),
    );
    return () => {
      live = false;
    };
  }, [app, org, wg]);
  if (pulled === 'loading') return <p data-testid="wg-settings.loading">{t('wg-settings.loading')}</p>;
  if (pulled !== 'ready')
    return (
      <p role="alert" data-testid="wg-settings.load-failed">
        {error(pulled.code).title}
      </p>
    );
  return (
    <>
      {current.status === 'none' && <p data-testid="wg-settings.defaults">{t('wg-settings.defaults')}</p>}
      {current.status === 'invalid' && (
        <p role="alert" data-testid="wg-settings.invalid">
          {t('wg-settings.invalid')}
        </p>
      )}
      {writer ? (
        <Form org={org} wg={wg} />
      ) : (
        <>
          <p data-testid="wg-settings.read-only">{t('wg-settings.read-only')}</p>
          <ReadOnly settings={settingsOf(current)} />
        </>
      )}
    </>
  );
}

export function WgSettingsScreen() {
  const { org, wg } = useParams() as { readonly org: string; readonly wg: string };
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  if (state === null)
    return (
      <p className="p-4" data-testid="wg-settings.unverified">
        {t('wg-settings.unverified')}
      </p>
    );
  if (userId === null || !state.canRead(userId, WG, wg))
    return (
      <p className="p-4" data-testid="wg-settings.not-found">
        {t('wg-settings.not-found')}
      </p>
    );
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="wg-settings.screen">
      <Link
        className="min-h-11 self-start py-2 text-sm text-blue-800 underline"
        to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(wg)}`}
        data-testid="wg-settings.back"
      >
        {t('wg-settings.back')}
      </Link>
      <h1 className="text-xl font-semibold" data-testid="wg-settings.title">
        {t('wg-settings.title')}
        {directory.workgroups.has(wg) && (
          <span className="block text-base font-normal">{directory.workgroups.get(wg)}</span>
        )}
      </h1>
      <Settings key={wg} org={org} wg={wg} writer={state.canWriteWg(userId, wg)} />
      <GithubDefault
        org={org}
        wg={wg}
        orgName={directory.orgName}
        editable={state.isAdmin(userId) || state.canWriteWg(userId, wg)}
      />
    </main>
  );
}
