// Platform invitations (06-web.md section 7.4, route `/platform/invites`, right platform:inviter; ADR-026). The right
// comes from the verified platform log; anyone else gets a neutral "not available" page (06-web.md section 12.6,
// without revealing more). An inviter creates an invitation – the code is drawn in the browser and only its ID and the
// expiry are sent – and sees the link `<origin>/invite#<code>` and the code once (copy buttons, confidentiality
// warning); the list shows open, used, revoked and expired invitations, open ones can be revoked. The rows are a
// `WindowedList` (ADR-086 section 1: at most `MAX_ROWS` rendered) over the hub's list read page by page ("Load more");
// a capped answer of an old hub says so.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import type { ListPaging } from '../../../core/lists/window';
import { actionErrorCode } from '../../../core/org/batch';
import {
  createPlatformInvite,
  listPlatformInvites,
  type PlatformInviteRow,
  preparePlatformInvite,
  type PreparedPlatformInvite,
  revokePlatformInvite,
  USER_EXPIRY_HOURS,
} from '../../../core/platform/invites';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { NotAvailable } from '../errors/pages';
import { useNow } from '../../countdown/useNow';
import { WindowedList } from '../../lists/WindowedList';
import { BUTTON, PRIMARY } from '../members/ActionDialog';
import { MAX_ROWS, PAGE, sameValue } from '../members/lists';
import { usePagedList } from '../members/usePagedList';
import { CopyButton, ExpirySelect } from '../devices/parts';
import { useInviter } from './useInviter';

function CreateForm(props: {
  readonly onCreated: (created: PreparedPlatformInvite) => void;
  readonly onCancel: () => void;
}) {
  const { app, origin } = useAppEnv();
  const { t, error } = useI18n();
  const [hours, setHours] = useState<number>(USER_EXPIRY_HOURS);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  // drawn once: "Try again" after a lost answer sends the identical request, so the expiry is fixed after the first
  // attempt (another expiry would create a second invitation while the first one may already exist)
  const prepared = useRef<PreparedPlatformInvite | null>(null);
  const [attempted, setAttempted] = useState(false);
  const submit = async () => {
    setBusy(true);
    setCode(null);
    setAttempted(true);
    try {
      prepared.current ??= await preparePlatformInvite({ hours, now: Date.now(), origin });
      await createPlatformInvite(app.client, prepared.current);
      props.onCreated(prepared.current);
    } catch (failure) {
      setCode(actionErrorCode(failure));
      setBusy(false);
    }
  };
  return (
    <form
      className="flex flex-col gap-3 rounded border border-gray-200 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      data-testid="platform-invites.form"
    >
      <ExpirySelect
        label={t('platform-invites.expiry')}
        disabled={attempted}
        value={hours}
        onChange={setHours}
        testId="platform-invites.expiry"
      />
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="platform-invites.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button type="submit" className={PRIMARY} disabled={busy} data-testid="platform-invites.submit">
          {t('platform-invites.create.submit')}
        </button>
        <button type="button" className={BUTTON} onClick={props.onCancel} data-testid="platform-invites.cancel">
          {t('platform-invites.cancel')}
        </button>
      </div>
    </form>
  );
}

function Result(props: { readonly created: PreparedPlatformInvite; readonly onDone: () => void }) {
  const { created } = props;
  const { t } = useI18n();
  return (
    <section
      className="flex flex-col gap-3 rounded border border-amber-400 bg-amber-50 p-4"
      data-testid="platform-invites.result"
    >
      <h2 className="font-semibold">{t('platform-invites.result.title')}</h2>
      <p role="note" data-testid="platform-invites.warning">
        {t('platform-invites.result.warning')}
      </p>
      <p className="break-all font-mono text-sm" data-testid="platform-invites.link">
        {created.link}
      </p>
      <CopyButton
        text={created.link}
        label={t('platform-invites.copy-link')}
        copiedLabel={t('platform-invites.copied')}
        testId="platform-invites.copy-link"
      />
      <p className="font-mono text-lg" data-testid="platform-invites.code">
        {created.code}
      </p>
      <CopyButton
        text={created.code}
        label={t('platform-invites.copy-code')}
        copiedLabel={t('platform-invites.copied')}
        testId="platform-invites.copy-code"
      />
      <button type="button" className={PRIMARY} onClick={props.onDone} data-testid="platform-invites.done">
        {t('platform-invites.done')}
      </button>
    </section>
  );
}

/** What an invitation row shows. */
interface InviteEntry {
  readonly row: PlatformInviteRow;
  readonly busy: boolean;
  readonly onRevoke: (id: string) => void;
}

function InviteRowView({ item }: { readonly item: InviteEntry }) {
  const { row } = item;
  const { t, locale } = useI18n();
  const now = useNow(false);
  const time = (text: string) => formatEventTime(Date.parse(text), { locale, timeZone: localTimeZone(), now });
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-gray-100 px-2 text-sm" data-testid="platform-invites.item.state">
          {t(`platform-invites.state.${row.state}`)}
        </span>
        <span className="text-sm">{t('platform-invites.list.created', { time: time(row.created_at) })}</span>
        <span className="text-sm">{t('platform-invites.list.expires', { time: time(row.expires_at) })}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm text-gray-700">
        {row.used_at !== null && <span>{t('platform-invites.list.used', { time: time(row.used_at) })}</span>}
        {row.revoked_at !== null && <span>{t('platform-invites.list.revoked', { time: time(row.revoked_at) })}</span>}
        {row.registered && row.used_at === null && <span>{t('platform-invites.list.registered')}</span>}
      </div>
      {row.state === 'open' && (
        <button
          type="button"
          className={`${BUTTON} self-start`}
          disabled={item.busy}
          onClick={() => item.onRevoke(row.invite_id)}
          data-testid="platform-invites.item.revoke"
        >
          {t('platform-invites.revoke')}
        </button>
      )}
    </>
  );
}

const inviteKey = (entry: InviteEntry) => entry.row.invite_id;
const inviteAttributes = (entry: InviteEntry) => ({
  'data-testid': 'platform-invites.item',
  'data-invite': entry.row.invite_id,
  'data-state': entry.row.state,
});

/** The invitation rows in the hub's order (newest first), windowed, with "Load more" of the hub's next page. */
export function PlatformInviteRows(props: {
  readonly rows: readonly PlatformInviteRow[];
  readonly busy: boolean;
  readonly onRevoke: (id: string) => void;
  readonly paging?: ListPaging;
}) {
  const { rows, busy, onRevoke, paging } = props;
  const entries = useMemo(() => rows.map((row): InviteEntry => ({ row, busy, onRevoke })), [rows, busy, onRevoke]);
  return (
    <WindowedList
      listId="platform-invites"
      items={entries}
      getKey={inviteKey}
      Row={InviteRowView}
      same={sameValue}
      newest="start"
      page={PAGE}
      maxRows={MAX_ROWS}
      testId="platform-invites.rows"
      rowClassName="flex flex-col gap-1 border-b border-gray-200 py-3"
      rowAttributes={inviteAttributes}
      {...(paging !== undefined && { hasMore: paging.hasMore, loading: paging.loading, onLoadMore: paging.loadMore })}
    />
  );
}

const platformKey = (row: PlatformInviteRow) => row.invite_id;
const NO_ROWS: readonly PlatformInviteRow[] = [];

/** The hub's list page by page (`usePagedList`); the loaded range is read again after a creation or a revocation. */
function InviteList({ version }: { readonly version: number }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const [failed, setFailed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const read = useCallback((before: string | null) => listPlatformInvites(app.client, before), [app]);
  const list = usePagedList(read, platformKey, `${version}/${reload}`);
  const revoke = useCallback(
    (id: string) => {
      setBusy(true);
      setFailed(null);
      void revokePlatformInvite(app.client, id)
        .then(
          () => setReload((n) => n + 1),
          (failure: unknown) => setFailed(actionErrorCode(failure)),
        )
        .finally(() => setBusy(false));
    },
    [app],
  );
  const code = failed ?? list.code;
  const rows = list.rows ?? NO_ROWS;
  return (
    <section className="flex flex-col gap-2" data-testid="platform-invites.list">
      <h2 className="font-semibold">{t('platform-invites.list.title')}</h2>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="platform-invites.list.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      {list.rows !== null && rows.length === 0 && (
        <p className="text-gray-600" data-testid="platform-invites.list.empty">
          {t('platform-invites.list.empty')}
        </p>
      )}
      {list.truncated && (
        <p className="rounded bg-gray-100 p-3 text-gray-800" role="note" data-testid="platform-invites.list.truncated">
          {t('platform-invites.list.truncated')}
        </p>
      )}
      <PlatformInviteRows rows={rows} busy={busy} onRevoke={revoke} paging={list.paging} />
    </section>
  );
}

export function PlatformInvitesScreen() {
  const { t } = useI18n();
  const { app } = useAppEnv();
  const inviter = useInviter();
  // the platform log is verified by the log job of an org; a fresh session may not have run it yet
  const activeOrg = useStore((state) => state.session.activeOrg, app.store);
  useEffect(() => {
    if (inviter === null && activeOrg !== null) app.tasks.refreshLog(activeOrg).catch(() => false);
  }, [app, inviter, activeOrg]);
  const [mode, setMode] = useState<'closed' | 'form'>('closed');
  const [created, setCreated] = useState<PreparedPlatformInvite | null>(null);
  const [version, setVersion] = useState(0);
  if (inviter === null)
    return (
      <p className="p-4" data-testid="platform-invites.loading">
        {t('platform-invites.loading')}
      </p>
    );
  // anyone without the right sees the neutral "not available" page (06-web.md section 12.6)
  if (!inviter) return <NotAvailable testId="platform-invites.forbidden" />;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="platform-invites.screen">
      <h1 className="text-xl font-semibold">{t('platform-invites.title')}</h1>
      {created !== null && <Result created={created} onDone={() => setCreated(null)} />}
      {created === null && mode === 'closed' && (
        <button
          type="button"
          className={`${PRIMARY} self-start`}
          onClick={() => setMode('form')}
          data-testid="platform-invites.open"
        >
          {t('platform-invites.create.open')}
        </button>
      )}
      {created === null && mode === 'form' && (
        <CreateForm
          onCreated={(invite) => {
            setCreated(invite);
            setMode('closed');
            setVersion((n) => n + 1);
          }}
          onCancel={() => setMode('closed')}
        />
      )}
      <InviteList version={version} />
    </main>
  );
}
