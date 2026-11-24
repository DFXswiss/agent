// The community screen (14-community.md sections 4.2 and 6), route `/community`: one group chat of every user of the
// platform, independent of the org in view. The screen starts the community client of this sign-in if the background
// job has not (`Community.start` runs once) and keeps a live channel open (the landing org's, if no org is active), so
// new messages, removals and mutes arrive without a reload.
//
// States: loading; a hub without the community; errors through the error catalog with "Try again"; waiting for the
// key ("Community is being unlocked …", after two minutes "… as soon as another user is online"); a failed key; empty;
// muted (no composer). "Load older messages" at the top; moderators see the muted users. While the page is visible
// the read position follows the newest message shown.
import { useEffect, useState, useSyncExternalStore } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { type Community, codeOf, community } from '../../../core/community/engine';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { loginTarget } from '../../shell/guard';
import { Composer } from './Composer';
import { Messages } from './Messages';
import { MutedUsers } from './MutedUsers';

/** After this long without a key the waiting text says that another user has to be online. */
export const LONG_WAIT_MS = 120_000;

/** The community client of the signed-in browser (the job's, or created here). */
export function useCommunity(): Community {
  const { app } = useAppEnv();
  // a new sign-in renews the client (`community` compares the store's generation)
  useStore((state) => state.me.userId, app.store);
  return community({ store: app.store, client: app.client, prefs: app.prefs });
}

function subscribeVisibility(changed: () => void): () => void {
  document.addEventListener('visibilitychange', changed);
  return () => document.removeEventListener('visibilitychange', changed);
}

/** Whether the page is visible (not a background tab, not minimised). */
export function usePageVisible(): boolean {
  return useSyncExternalStore(subscribeVisibility, () => document.visibilityState === 'visible');
}

function Waiting({ since }: { readonly since: number | null }) {
  const { t } = useI18n();
  const now = useNow(true, 5_000);
  const long = since !== null && now - since >= LONG_WAIT_MS;
  return (
    <p
      role="status"
      className="rounded border border-blue-200 bg-blue-50 p-3"
      data-testid="community.waiting"
      data-long={long}
    >
      {t(long ? 'community.waiting.long' : 'community.waiting')}
    </p>
  );
}

function Older({ engine }: { readonly engine: Community }) {
  const { t, error } = useI18n();
  const [busy, setBusy] = useState(false);
  // the failure text, empty while there is none (kept as text: no branch in the markup, ADR-069 web coverage)
  const [failure, setFailure] = useState('');
  const load = async () => {
    setBusy(true);
    setFailure('');
    try {
      await engine.loadOlder();
    } catch (caught) {
      setFailure(error(codeOf(caught)).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col items-center gap-1">
      <button
        type="button"
        className="min-h-11 rounded border border-gray-300 px-4 text-sm disabled:opacity-50"
        disabled={busy}
        onClick={() => void load()}
        data-testid="community.older"
      >
        {busy ? t('community.older.loading') : t('community.older')}
      </button>
      <p role="alert" hidden={failure === ''} className="text-sm text-red-800" data-testid="community.older.error">
        {failure}
      </p>
    </div>
  );
}

function Ready({ engine }: { readonly engine: Community }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const state = useStore((s) => s.community, app.store);
  const visible = usePageVisible();
  const [notice, setNotice] = useState<string | null>(null);
  const newest = state.messages[state.messages.length - 1]?.seq ?? 0;
  useEffect(() => {
    if (!visible || newest <= state.lastRead) return;
    engine.markRead(newest).catch(() => undefined);
  }, [engine, visible, newest, state.lastRead]);
  const firstPost = !state.posted && !state.messages.some((message) => message.author === state.me);
  return (
    <>
      {state.moderator && (
        <div className="flex justify-end">
          <MutedUsers engine={engine} />
        </div>
      )}
      {notice !== null && (
        <p role="status" className="rounded border border-green-200 bg-green-50 p-3" data-testid="community.status">
          {notice}
        </p>
      )}
      {state.older && <Older engine={engine} />}
      {state.loaded && state.messages.length === 0 && (
        <p className="py-8 text-center text-gray-600" data-testid="community.empty">
          {t('community.empty')}
        </p>
      )}
      <Messages
        messages={state.messages}
        me={state.me}
        moderator={state.moderator}
        engine={engine}
        onNotice={setNotice}
      />
      {(state.key === 'none' || state.key === 'waiting') && <Waiting since={state.waitingSince} />}
      {state.key === 'failed' && (
        <div
          role="alert"
          className="rounded border border-red-200 bg-red-50 p-3 text-red-900"
          data-testid="community.key-failed"
        >
          <p className="font-semibold">{t('community.key-failed')}</p>
          <p>{error(state.keyCode!).body}</p>
        </div>
      )}
      {state.muted ? (
        <p role="status" className="rounded border border-amber-300 bg-amber-50 p-3" data-testid="community.muted">
          {t('community.muted')}
        </p>
      ) : (
        state.key === 'ready' && <Composer engine={engine} firstPost={firstPost} />
      )}
    </>
  );
}

export function CommunityScreen() {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const location = useLocation();
  const unlocked = useStore((state) => state.session.phase === 'unlocked', app.store);
  const phase = useStore((state) => state.community.phase, app.store);
  const code = useStore((state) => state.community.code, app.store);
  const engine = useCommunity();
  useEffect(() => {
    if (!unlocked) return;
    void engine.start();
    // the live channel follows the active org; without one, the landing org's channel carries the community frames
    if (app.store.getState().session.activeOrg !== null) return;
    const org = app.landingOrg();
    if (org !== null) app.activate(org);
  }, [app, engine, unlocked]);
  if (!unlocked) return <Navigate to={loginTarget(location.pathname, location.search)} replace />;
  return (
    <main
      className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4"
      data-testid="community.screen"
      data-phase={phase}
    >
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">{t('community.title')}</h1>
        <p className="text-sm text-gray-700">{t('community.intro')}</p>
      </header>
      {(phase === 'idle' || phase === 'loading') && (
        <p role="status" data-testid="community.loading">
          {t('community.loading')}
        </p>
      )}
      {phase === 'absent' && <p data-testid="community.absent">{t('community.absent')}</p>}
      {phase === 'error' && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded border border-red-200 bg-red-50 p-3 text-red-900"
          data-testid="community.error"
          data-code={code}
        >
          <p className="font-semibold">{error(code!).title}</p>
          <p>{error(code!).body}</p>
          <button
            type="button"
            className="min-h-11 self-start rounded border border-red-300 bg-white px-4"
            onClick={() => void engine.start()}
            data-testid="community.retry"
          >
            {t('community.retry')}
          </button>
        </div>
      )}
      {phase === 'ready' && <Ready engine={engine} />}
    </main>
  );
}
