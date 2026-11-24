// The open invitations of an organization (06-web.md section 7.3): the device invitations in "Add org device" (the
// member screen shows the user invitations with their status, `InviteSection`). The hub's list is checked against the verified log (`openInvites`), newest
// first; open ones can be revoked. The rows are a `WindowedList` (ADR-086 section 1) over the hub's list read page by
// page ("Load more", `usePagedList`); a capped answer of an old hub says so.
import { useCallback, useMemo, useState } from 'react';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import type { ListPaging } from '../../../core/lists/window';
import { actionErrorCode } from '../../../core/org/batch';
import { type InviteRow, type OpenInvite, openInvites, revokeInvite } from '../../../core/org/invites';
import { useNow } from '../../countdown/useNow';
import { useI18n } from '../../i18n';
import { WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { BUTTON } from './ActionDialog';
import { MAX_ROWS, PAGE, sameValue } from './lists';
import { PagedNotes } from './PagedNotes';
import { usePagedList } from './usePagedList';

type Prefix = 'members.invites' | 'devices.invites';

const NO_ROWS: readonly OpenInvite[] = [];

interface InviteEntry {
  readonly row: InviteRow;
  readonly prefix: Prefix;
  readonly busy: boolean;
  readonly onRevoke: (id: string) => void;
}

function InviteRowView({ item }: { readonly item: InviteEntry }) {
  const { row, prefix } = item;
  const { t, locale } = useI18n();
  const now = useNow(false);
  const time = formatEventTime(Date.parse(row.expires_at), { locale, timeZone: localTimeZone(), now });
  return (
    <>
      <span>{t(`${prefix}.expires`, { time })}</span>
      <button
        type="button"
        className={BUTTON}
        disabled={item.busy}
        onClick={() => item.onRevoke(row.invite_id)}
        data-testid={`${prefix}.revoke`}
      >
        {t(`${prefix}.revoke`)}
      </button>
    </>
  );
}

const inviteKey = (entry: InviteEntry) => entry.row.invite_id;
const inviteAttributes = (entry: InviteEntry) => ({ 'data-testid': `${entry.prefix}.item` });

/** The rows of open invitations. */
export function InviteList(props: {
  readonly prefix: Prefix;
  readonly rows: readonly InviteRow[];
  readonly busy: boolean;
  readonly onRevoke: (id: string) => void;
  readonly paging?: ListPaging;
}) {
  const { prefix, rows, busy, onRevoke, paging } = props;
  const entries = useMemo(
    () => rows.map((row): InviteEntry => ({ row, prefix, busy, onRevoke })),
    [rows, prefix, busy, onRevoke],
  );
  return (
    <WindowedList
      listId={prefix}
      items={entries}
      getKey={inviteKey}
      Row={InviteRowView}
      same={sameValue}
      newest="start"
      page={PAGE}
      maxRows={MAX_ROWS}
      testId={`${prefix}.rows`}
      className="flex flex-col gap-2"
      rowClassName="flex flex-wrap items-center gap-3"
      rowAttributes={inviteAttributes}
      rowHeight="sm"
      {...(paging !== undefined && { hasMore: paging.hasMore, loading: paging.loading, onLoadMore: paging.loadMore })}
    />
  );
}

const openKey = (row: OpenInvite) => row.invite_id;

/**
 * The open device invitations, page by page (`usePagedList`): the loaded range is read again whenever `version`
 * changes and after a revocation.
 */
export function OpenInvites({ org, version }: { readonly org: string; readonly version: string | number }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const prefix: Prefix = 'devices.invites';
  const [failed, setFailed] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [reload, setReload] = useState(0);
  const read = useCallback((before: string | null) => openInvites(app.tasks, org, 'device', before), [app, org]);
  const list = usePagedList(read, openKey, `${version}/${reload}`);
  const revoke = useCallback(
    (id: string) => {
      setRevoking(true);
      setFailed(null);
      void revokeInvite(app.tasks, org, id)
        .then(
          () => setReload((n) => n + 1),
          (failure: unknown) => setFailed(actionErrorCode(failure)),
        )
        .finally(() => setRevoking(false));
    },
    [app, org],
  );
  const code = failed ?? list.code;
  const rows = list.rows ?? NO_ROWS;
  return (
    <section className="flex flex-col gap-2" data-testid={prefix}>
      <h2 className="font-semibold">{t(`${prefix}.title`)}</h2>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid={`${prefix}.error`} data-code={code}>
          {error(code).title}
        </p>
      )}
      {list.rows !== null && rows.length === 0 && !list.more && (
        <p className="text-gray-600" data-testid={`${prefix}.empty`}>
          {t(`${prefix}.empty`)}
        </p>
      )}
      <PagedNotes
        list={list}
        shown={rows.length}
        note={`${prefix}.truncated`}
        rows={`${prefix}.rows`}
        testId={prefix}
      />
      <InviteList prefix={prefix} rows={rows} busy={revoking} onRevoke={revoke} paging={list.paging} />
    </section>
  );
}
