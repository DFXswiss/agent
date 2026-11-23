// The muted users of the community for moderators (12-community.md section 6): each with the last known name (from
// the loaded messages) and the sender mark, else the mark alone, the time of the mute and "Unmute".
import { useEffect, useState } from 'react';
import { type Community, codeOf } from '../../../core/community/engine';
import type { CommunityMessage } from '../../../core/store/slices/community';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { Sheet } from '../../responsive/Sheet';
import { useAppEnv } from '../../shell/context';
import { fullTime, Mark } from './parts';

type Mutes = Awaited<ReturnType<Community['mutes']>>;

/** The newest display name of `user` among the loaded messages, null without one. */
export function lastName(messages: readonly CommunityMessage[], user: string): string | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!;
    if (message.author === user && message.name !== null) return message.name;
  }
  return null;
}

function MutedList({ engine }: { readonly engine: Community }) {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const messages = useStore((state) => state.community.messages, app.store);
  const [mutes, setMutes] = useState<Mutes | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // bumped after an unmute: the list is read again
  const [version, setVersion] = useState(0);
  useEffect(() => {
    // a state update after the sheet closed is ignored (React 18)
    engine.mutes().then(
      (rows) => {
        setMutes(rows);
        setCode(null);
      },
      (failure: unknown) => setCode(codeOf(failure)),
    );
  }, [engine, version]);
  const unmute = async (user: string) => {
    setBusy(true);
    try {
      await engine.setMuted(user, false);
      setVersion((count) => count + 1);
    } catch (failure) {
      setCode(codeOf(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      {code !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="community.mutes.error">
          {error(code).title}
        </p>
      )}
      {mutes === null && code === null && <p data-testid="community.mutes.loading">{t('community.mutes.loading')}</p>}
      {mutes?.length === 0 && <p data-testid="community.mutes.empty">{t('community.mutes.empty')}</p>}
      {mutes !== null && mutes.length > 0 && (
        <ul className="flex flex-col divide-y divide-gray-200" data-testid="community.mutes.list">
          {mutes.map((mute) => {
            const name = lastName(messages, mute.user);
            return (
              <li key={mute.user} className="flex flex-wrap items-center gap-2 py-2" data-testid="community.mutes.item">
                {name !== null && (
                  <span className="break-all font-medium" data-testid="community.mutes.name">
                    {name}
                  </span>
                )}
                <Mark user={mute.user} testId="community.mutes.mark" />
                {mute.mutedAt > 0 && (
                  <span className="text-xs text-gray-600" data-testid="community.mutes.time">
                    {t('community.mutes.since', { time: fullTime(mute.mutedAt, locale) })}
                  </span>
                )}
                <button
                  type="button"
                  className="ml-auto min-h-11 rounded border border-gray-300 px-3 disabled:opacity-50"
                  disabled={busy}
                  onClick={() => void unmute(mute.user)}
                  data-testid="community.mutes.unmute"
                >
                  {t('community.mutes.unmute')}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

export function MutedUsers({ engine }: { readonly engine: Community }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="min-h-11 rounded border border-gray-300 px-3 text-sm"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        data-testid="community.mutes.open"
      >
        {t('community.mutes.open')}
      </button>
      {open && (
        <Sheet title={t('community.mutes.title')} testId="community.mutes" onClose={() => setOpen(false)}>
          <MutedList engine={engine} />
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-4"
            onClick={() => setOpen(false)}
            data-testid="community.mutes.close"
          >
            {t('community.close')}
          </button>
        </Sheet>
      )}
    </>
  );
}
