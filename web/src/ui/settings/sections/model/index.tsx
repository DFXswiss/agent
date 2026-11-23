// Settings section "Model access" (06-web.md section 8.2, ADR-010): the preference (automatic, Claude subscription,
// Anthropic API key, DFX platform) and the model, saved in the user's settings (`PUT /v1/me/settings`). The models come
// only from the hub's configuration (`GET /v1/config`, `models` per access kind; `core/credentials/model.ts`); a model
// the new preference does not allow falls back to the default. The platform is available only on DFX devices.
import { type FormEvent, useState } from 'react';
import {
  ACCESS_KINDS,
  type AccessPreference,
  modelChoices,
  modelSettings,
  platformDefault,
  saveModelSettings,
} from '../../../../core/credentials/model';
import { requestErrorCode } from '../../../../core/requests/client';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';

const DEFAULT = '';

type Status = 'idle' | 'saving' | 'saved' | { readonly code: string };

export function ModelSection() {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const reason = useReason();
  const config = useStore((state) => state.config.config, app.store);
  const [settings, setSettings] = useState(() => modelSettings(app.store));
  const [status, setStatus] = useState<Status>('idle');
  const choices = modelChoices(config, settings.access);
  const fallback = platformDefault(config);
  const changeAccess = (access: AccessPreference) => {
    setStatus('idle');
    setSettings({
      access,
      modelId:
        settings.modelId !== null && modelChoices(config, access).includes(settings.modelId) ? settings.modelId : null,
    });
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setStatus('saving');
    try {
      setSettings(await saveModelSettings(app.tasks, settings, locale));
      setStatus('saved');
    } catch (error) {
      setStatus({ code: requestErrorCode(error) });
    }
  };
  return (
    <form className="flex flex-col gap-3" onSubmit={(event) => void save(event)} data-testid="model.form">
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1">{t('model.access.label')}</legend>
        {ACCESS_KINDS.map((access) => (
          <label key={access} className="flex min-h-11 items-start gap-2">
            <input
              type="radio"
              name="model-access"
              className="mt-1"
              checked={settings.access === access}
              onChange={() => changeAccess(access)}
              data-testid={`model.access.${access}`}
            />
            <span className="flex flex-col">
              <span>{t(`model.access.${access}`)}</span>
              <span className="text-sm text-gray-600">{t(`model.access.${access}.hint`)}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <p className="text-sm text-gray-600" data-testid="model.platform-note">
        {t('model.platform-note')}
      </p>
      <label htmlFor="model-id">{t('model.model.label')}</label>
      <select
        id="model-id"
        className="min-h-11 self-start rounded border border-gray-400 px-3"
        value={settings.modelId ?? DEFAULT}
        onChange={(event) =>
          setSettings({ ...settings, modelId: event.target.value === DEFAULT ? null : event.target.value })
        }
        data-testid="model.model"
      >
        <option value={DEFAULT}>
          {settings.access === 'platform' && fallback !== null
            ? t('model.model.default-named', { model: fallback })
            : t('model.model.default')}
        </option>
        {choices.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
      {choices.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="model.model.none">
          {t('model.model.none')}
        </p>
      )}
      <button
        type="submit"
        className="min-h-11 self-start rounded bg-blue-700 px-4 text-white"
        disabled={status === 'saving'}
        data-testid="model.save"
      >
        {t('model.save')}
      </button>
      {status === 'saved' && (
        <p role="status" data-testid="model.saved">
          {t('model.saved')}
        </p>
      )}
      {typeof status === 'object' && (
        <p role="alert" data-testid="model.failed" data-code={status.code}>
          {t('model.failed', { reason: reason(status.code) })}
        </p>
      )}
    </form>
  );
}

export const section: SettingsSection = { id: 'model', order: 60, titleKey: 'model.title', Component: ModelSection };
