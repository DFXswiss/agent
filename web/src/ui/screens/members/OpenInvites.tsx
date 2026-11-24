// The open invitations of an organization (06-web.md sections 7.1 and 7.3): user invitations on the member screen,
// device invitations in "Add org device". The hub's list is checked against the verified log (`openInvites`), newest
// first; open ones can be revoked. The rows are a `WindowedList` (ADR-063 section 1); `paging` is the server paging of
// the hub's list once its loader offers it.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import type { ListPaging } from '../../../core/lists/paging';
import { actionErrorCode } from '../../../core/org/batch';
import { type InviteRow, openInvites, revokeInvite } from '../../../core/org/invites';
import { useNow } from '../../countdown/useNow';
import { useI18n } from '../../i18n';
import { WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { BUTTON } from './ActionDialog';
import { MAX_ROWS, PAGE, sameValue } from './lists';

type Prefix = 'members.invites' | 'devices.invites';

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

/** The open invitations of `kind`, read again whenever `version` changes and after a revocation. */
export function OpenInvites({
  org,
  kind,
  version,
}: {
  readonly org: string;
  readonly kind: 'user' | 'device';
  readonly version: string | number;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const prefix: Prefix = kind === 'user' ? 'members.invites' : 'devices.invites';
  const [rows, setRows] = useState<InviteRow[] | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    // a late answer after unmounting sets nothing (React ignores it); the list is reloaded on every change anyway
    openInvites(app.tasks, org, kind).then(
      (found) => {
        setRows(found);
        setCode(null);
      },
      (failure: unknown) => setCode(actionErrorCode(failure)),
    );
  }, [app, org, kind, version, reload]);
  const revoke = useCallback(
    (id: string) => {
      setRevoking(true);
      void revokeInvite(app.tasks, org, id)
        .then(
          () => setReload((n) => n + 1),
          (failure: unknown) => setCode(actionErrorCode(failure)),
        )
        .finally(() => setRevoking(false));
    },
    [app, org],
  );
  return (
    <section className="flex flex-col gap-2" data-testid={prefix}>
      <h2 className="font-semibold">{t(`${prefix}.title`)}</h2>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid={`${prefix}.error`} data-code={code}>
          {error(code).title}
        </p>
      )}
      {rows !== null && rows.length === 0 && (
        <p className="text-gray-600" data-testid={`${prefix}.empty`}>
          {t(`${prefix}.empty`)}
        </p>
      )}
      <InviteList prefix={prefix} rows={rows ?? []} busy={revoking} onRevoke={revoke} />
    </section>
  );
}
