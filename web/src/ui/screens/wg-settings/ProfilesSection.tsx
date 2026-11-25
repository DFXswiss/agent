// Rights profiles of a workgroup (ADR-090 section 2): the built-in roles and the own profiles of the organization and
// the workgroup with their operation types and read reach; writers of the workgroup add or remove a workgroup
// profile, admins an organization profile. A profile is a voluntary restriction a person assigns to a task; nothing
// here restricts anybody by itself (E-11).
import { useCallback, useEffect, useState } from 'react';
import {
  deleteProfile,
  listProfiles,
  NAME_RE,
  REACHES,
  type ProfileList,
  type Reach,
  saveProfile,
} from '../../../core/profiles/profiles';
import { sendErrorCode } from '../../../core/tasks/send';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';

export function ProfilesSection({
  org,
  wg,
  writer,
  admin,
}: {
  readonly org: string;
  readonly wg: string;
  readonly writer: boolean;
  readonly admin: boolean;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const [list, setList] = useState<ProfileList | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [operations, setOperations] = useState<readonly string[]>([]);
  const [reads, setReads] = useState<Reach>('all');
  const [scope, setScope] = useState<'wg' | 'org'>('wg');
  const reload = useCallback(() => {
    listProfiles(app.tasks, wg).then(setList, (caught: unknown) => setFailed(sendErrorCode(caught)));
  }, [app, wg]);
  useEffect(reload, [reload]);
  const run = (work: Promise<void>) => {
    setFailed(null);
    work.then(reload, (caught: unknown) => setFailed(sendErrorCode(caught)));
  };
  const types = Object.keys(list?.classes ?? {}).sort();
  const valid = NAME_RE.test(name) && (scope === 'wg' ? writer : admin);
  return (
    <section className="flex flex-col gap-2" data-testid="wg-settings.profiles">
      <h2 className="text-lg font-semibold">{t('profiles.title')}</h2>
      <p className="text-sm text-gray-600">{t('profiles.hint')}</p>
      {failed !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="wg-settings.profiles.error">
          {error(failed).title}
        </p>
      )}
      <ul className="flex flex-col gap-1">
        {(list?.profiles ?? []).map((item) => (
          <li
            key={`${item.scope} ${item.name}`}
            className="flex flex-wrap items-center gap-2 text-sm"
            data-testid="wg-settings.profile"
            data-name={item.name}
            data-scope={item.scope}
          >
            <span className="font-medium">{item.name}</span>
            <span className="text-gray-600">{t(`profiles.scope.${item.scope}`)}</span>
            <span className="text-gray-600">{t(`profiles.reads.${item.reads}`)}</span>
            <span className="text-gray-600">{item.operations.join(', ')}</span>
            {((item.scope === 'wg' && writer) || (item.scope === 'org' && admin)) && (
              <button
                type="button"
                className={BUTTON}
                onClick={() =>
                  run(deleteProfile(app.tasks, item.scope as 'wg' | 'org', item.scope === 'wg' ? wg : org, item.name))
                }
                data-testid="wg-settings.profile.delete"
              >
                {t('profiles.delete')}
              </button>
            )}
          </li>
        ))}
      </ul>
      {(writer || admin) && (
        <div className="flex flex-col gap-2 rounded border border-gray-200 p-3" data-testid="wg-settings.profile.form">
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2">
              {t('profiles.name')}
              <input
                className="min-h-11 rounded border border-gray-300 px-2"
                value={name}
                onChange={(event) => setName(event.target.value.trim())}
                spellCheck={false}
                data-testid="wg-settings.profile.name"
              />
            </label>
            <label className="flex items-center gap-2">
              {t('profiles.reads')}
              <select
                className="min-h-11 rounded border border-gray-300 px-2"
                value={reads}
                onChange={(event) => setReads(event.target.value as Reach)}
                data-testid="wg-settings.profile.reads"
              >
                {REACHES.map((reach) => (
                  <option key={reach} value={reach}>
                    {t(`profiles.reads.${reach}`)}
                  </option>
                ))}
              </select>
            </label>
            {admin && (
              <label className="flex items-center gap-2">
                {t('profiles.scope')}
                <select
                  className="min-h-11 rounded border border-gray-300 px-2"
                  value={scope}
                  onChange={(event) => setScope(event.target.value as 'wg' | 'org')}
                  data-testid="wg-settings.profile.scope"
                >
                  <option value="wg">{t('profiles.scope.wg')}</option>
                  <option value="org">{t('profiles.scope.org')}</option>
                </select>
              </label>
            )}
          </div>
          <fieldset className="flex flex-wrap gap-2 text-sm" data-testid="wg-settings.profile.types">
            <legend>{t('profiles.operations')}</legend>
            {types.map((type) => (
              <label key={type} className="flex min-h-11 items-center gap-1">
                <input
                  type="checkbox"
                  checked={operations.includes(type)}
                  onChange={(event) =>
                    setOperations(
                      event.target.checked ? [...operations, type] : operations.filter((item) => item !== type),
                    )
                  }
                  data-testid="wg-settings.profile.type"
                  data-type={type}
                />
                {type}
              </label>
            ))}
          </fieldset>
          <button
            type="button"
            className="min-h-11 self-start rounded bg-blue-700 px-3 text-white disabled:opacity-50"
            disabled={!valid}
            onClick={() => run(saveProfile(app.tasks, scope, scope === 'wg' ? wg : org, { name, operations, reads }))}
            data-testid="wg-settings.profile.save"
          >
            {t('profiles.save')}
          </button>
        </div>
      )}
    </section>
  );
}
