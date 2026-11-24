// Members and rights (06-web.md section 7.1, route `/o/:org/members`, right org:member, edit: admin). The list comes
// from the verified log (names: the member's own `member.profile`, else a provisional name an admin gave – marked with
// who gave it –, else "Name not known yet" with what happens next; the ID only on request, ADR-073); admins see the
// controls – enter or change a provisional name, – invite, change role (hidden for
// the last admin), grant workgroup access (optionally with history; every writable workgroup, the key holders deliver
// the key), ungrant an explicit access, remove – each change through the confirmation dialog ("Saving …",
// `submitBatch`). An access an admin has only by its role shows "reads as admin" and has nothing to ungrant (ADR-058).
// Every member can leave (hidden for the last admin). Controls the user's verified rights do not allow are not
// rendered (section 1.2). The rows are a `WindowedList` (ADR-086 section 1: at most `MAX_ROWS` rendered), built once
// per log change; a member's workgroup chips show the first `CHIP_LIMIT` and "N more – show".
import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { type Planner, submitBatch, type SubmitProgress } from '../../../core/org/batch';
import {
  type Access,
  canManage,
  grantable,
  grantPlan,
  leavePlan,
  type MemberAccess,
  type MemberRow,
  memberRows,
  removePlan,
  rolePlan,
  ungrantPlan,
} from '../../../core/org/members';
import type { OrgState } from '../../../core/log/state';
import type { Directory } from '../../../core/tasks/directory';
import { useI18n } from '../../i18n';
import { WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { ActionDialog, BUTTON } from './ActionDialog';
import { AdmitSection } from './AdmitSection';
import { Chips } from './Chips';
import { InviteSection } from './InviteSection';
import { NameDialog } from './NameDialog';
import { givenBy } from '../../names/names';
import { MAX_ROWS, PAGE, sameValue } from './lists';

/** The dialog that is open, with what it acts on. */
export type Open =
  | { readonly kind: 'role'; readonly member: MemberRow }
  | { readonly kind: 'remove'; readonly member: MemberRow }
  | { readonly kind: 'ungrant'; readonly member: MemberRow; readonly wg: string }
  | { readonly kind: 'grant'; readonly member: MemberRow }
  | { readonly kind: 'name'; readonly member: MemberRow }
  | { readonly kind: 'leave' };

/** A member's name as text: the own name, "<name> (provisional)", or "Name not known yet" (never an ID). */
export function memberName(t: (key: string, params?: Record<string, string>) => string, row: MemberRow): string {
  if (row.name === null) return t('names.person.unknown');
  return row.nameSource?.source === 'own' ? row.name : t('names.person.provisional', { name: row.name });
}

interface GrantDialogProps {
  readonly org: string;
  readonly member: MemberRow;
  readonly state: OrgState;
  readonly directory: Directory;
  readonly onClose: () => void;
}

function GrantDialog({ member, state, directory, onClose, org }: GrantDialogProps) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const choices = grantable(state, member.id);
  const [wg, setWg] = useState(choices[0]?.wg ?? '');
  const [access, setAccess] = useState<Access>('read');
  const [history, setHistory] = useState(false);
  return (
    <ActionDialog
      title={t('members.grant.title', { name: memberName(t, member) })}
      testId="members.grant"
      confirmLabel={t('members.grant.confirm')}
      ready={wg !== ''}
      run={(progress) => submitBatch(app.tasks, org, grantPlan(member.id, wg, access, history), progress)}
      onDone={onClose}
      onClose={onClose}
    >
      {choices.length === 0 ? (
        <p data-testid="members.grant.none">{t('members.grant.none')}</p>
      ) : (
        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span>{t('members.grant.workgroup')}</span>
            <select
              className="min-h-11 rounded border border-gray-300 px-2"
              value={wg}
              onChange={(event) => setWg(event.target.value)}
              data-testid="members.grant.wg"
            >
              {choices.map((choice) => (
                <option key={choice.wg} value={choice.wg}>
                  {directory.workgroups.get(choice.wg) ?? t('members.wg.unnamed')}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span>{t('members.grant.access')}</span>
            <select
              className="min-h-11 rounded border border-gray-300 px-2"
              value={access}
              onChange={(event) => setAccess(event.target.value as Access)}
              data-testid="members.grant.access"
            >
              <option value="read">{t('members.access.read')}</option>
              <option value="write">{t('members.access.write')}</option>
            </select>
          </label>
          <label className="flex min-h-11 items-center gap-2">
            <input
              type="checkbox"
              checked={history}
              onChange={(event) => setHistory(event.target.checked)}
              data-testid="members.grant.history"
            />
            <span>{t('members.grant.history')}</span>
          </label>
        </div>
      )}
    </ActionDialog>
  );
}

/** What a member row shows: the row, the viewer's rights, the names of the org (one selector for the list). */
interface MemberEntry {
  readonly row: MemberRow;
  readonly admin: boolean;
  readonly directory: Directory;
  readonly onOpen: (open: Open) => void;
}

function MemberRowView({ item }: { readonly item: MemberEntry }) {
  const { row, admin, directory, onOpen } = item;
  const { t } = useI18n();
  const manage = admin && !row.lastAdmin;
  const own = row.nameSource?.source === 'own';
  const given = row.name === null || own ? null : givenBy(t, directory, row.id);
  const wgName = (wg: string) => directory.workgroups.get(wg) ?? t('members.wg.unnamed');
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="font-medium"
          data-testid="members.item.name"
          data-source={row.nameSource?.source ?? 'none'}
          title={t('names.id.value', { id: row.id })}
        >
          {row.name ?? t('names.person.unknown')}
        </span>
        {row.isMe && <span className="text-sm text-gray-600">{t('members.me')}</span>}
        <span className="rounded bg-gray-100 px-2 text-sm" data-testid="members.item.role">
          {t(`members.role.${row.role}`)}
        </span>
      </div>
      {row.name === null && (
        <p className="text-sm text-gray-700" data-testid="members.item.unnamed">
          {t(admin ? 'names.person.unknown.hint.admin' : 'names.person.unknown.hint')}
        </p>
      )}
      {given !== null && (
        <p className="text-sm text-gray-700" data-testid="members.item.given">
          {given}
        </p>
      )}
      <details className="text-sm text-gray-600" data-testid="members.item.id">
        <summary className="min-h-11 cursor-pointer">{t('names.id.show')}</summary>
        <code className="break-all">{row.id}</code>
      </details>
      <Chips
        items={row.access}
        wgOf={accessWg}
        testId="members.item.access"
        more="members.access.more"
        render={(access) => (
          <>
            <span>{wgName(access.wg)}</span>
            <span className="text-gray-600" data-inherited={access.inherited}>
              {access.inherited ? t('members.access.inherited') : t(`members.access.${access.access}`)}
            </span>
            {admin && !access.isDefault && !access.inherited && (
              <button
                type="button"
                className="min-h-11 min-w-11 underline"
                onClick={() => onOpen({ kind: 'ungrant', member: row, wg: access.wg })}
                aria-label={t('members.ungrant.label', { name: memberName(t, row), wg: wgName(access.wg) })}
                data-testid="members.item.ungrant"
              >
                ×
              </button>
            )}
          </>
        )}
      />
      {admin && (
        <div className="flex flex-wrap gap-2">
          {!own && !row.isMe && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'name', member: row })}
              data-testid="members.item.give-name"
            >
              {row.name === null ? t('names.give.open') : t('names.give.change')}
            </button>
          )}
          {manage && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'role', member: row })}
              data-testid="members.item.role-change"
            >
              {row.role === 'admin' ? t('members.role.to-member') : t('members.role.to-admin')}
            </button>
          )}
          <button
            type="button"
            className={BUTTON}
            onClick={() => onOpen({ kind: 'grant', member: row })}
            data-testid="members.item.grant"
          >
            {t('members.grant.open')}
          </button>
          {!row.isMe && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'remove', member: row })}
              data-testid="members.item.remove"
            >
              {t('members.remove.open')}
            </button>
          )}
        </div>
      )}
    </>
  );
}

const accessWg = (access: MemberAccess) => access.wg;
const memberKey = (entry: MemberEntry) => entry.row.id;
const memberAttributes = (entry: MemberEntry) => ({ 'data-testid': 'members.item', 'data-member': entry.row.id });

/** The member rows in the order of `memberRows` (admins first, then by name), windowed. */
export function MemberList(props: {
  readonly rows: readonly MemberRow[];
  readonly admin: boolean;
  readonly directory: Directory;
  readonly onOpen: (open: Open) => void;
}) {
  const { rows, admin, directory, onOpen } = props;
  const entries = useMemo(
    () => rows.map((row): MemberEntry => ({ row, admin, directory, onOpen })),
    [rows, admin, directory, onOpen],
  );
  return (
    <WindowedList
      listId="members"
      items={entries}
      getKey={memberKey}
      Row={MemberRowView}
      same={sameValue}
      newest="start"
      page={PAGE}
      maxRows={MAX_ROWS}
      testId="members.list"
      labels={{ older: 'members.list.more', newer: 'members.list.above', newest: 'members.list.top' }}
      rowClassName="flex flex-col gap-2 border-b border-gray-200 py-3"
      rowAttributes={memberAttributes}
      rowHeight="lg"
    />
  );
}

export function MembersScreen() {
  const { org } = useParams() as { readonly org: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const navigate = useNavigate();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const [open, setOpen] = useState<Open | null>(null);
  // rebuilt only when the verified log, the user or the names change, not on every render
  const rows = useMemo(
    () => (state === null || userId === null ? [] : memberRows(state, userId, directory)),
    [state, userId, directory],
  );
  if (state === null) {
    return (
      <p className="p-4" data-testid="members.loading">
        {t('members.loading')}
      </p>
    );
  }
  if (userId === null || state.member(userId) === null) {
    return (
      <p className="p-4" data-testid="members.not-found">
        {t('members.not-found')}
      </p>
    );
  }
  const admin = canManage(state, userId);
  const me = rows.find((row) => row.isMe)!;
  const close = () => setOpen(null);
  const run = (planner: Planner) => (progress: (step: SubmitProgress) => void) =>
    submitBatch(app.tasks, org, planner, progress);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="members.screen">
      <h1 className="text-xl font-semibold">{t('members.title')}</h1>
      {admin && <AdmitSection org={org} state={state} directory={directory} />}
      {admin && <InviteSection org={org} state={state} directory={directory} />}
      <MemberList rows={rows} admin={admin} directory={directory} onOpen={setOpen} />
      {!me.lastAdmin && (
        <button
          type="button"
          className={`${BUTTON} self-start`}
          onClick={() => setOpen({ kind: 'leave' })}
          data-testid="members.leave"
        >
          {t('members.leave.open')}
        </button>
      )}
      {open?.kind === 'role' && (
        <ActionDialog
          title={t('members.role.title', { name: memberName(t, open.member) })}
          testId="members.role"
          confirmLabel={open.member.role === 'admin' ? t('members.role.to-member') : t('members.role.to-admin')}
          run={run(rolePlan(open.member.id, open.member.role === 'admin' ? 'member' : 'admin'))}
          onDone={close}
          onClose={close}
        >
          {open.member.role === 'admin' && <p data-testid="members.role.demote">{t('members.role.demote')}</p>}
        </ActionDialog>
      )}
      {open?.kind === 'remove' && (
        <ActionDialog
          title={t('members.remove.title', { name: memberName(t, open.member) })}
          testId="members.remove"
          confirmLabel={t('members.remove.confirm')}
          danger
          run={run(removePlan(open.member.id))}
          onDone={close}
          onClose={close}
        >
          <p>{t('members.remove.body')}</p>
        </ActionDialog>
      )}
      {open?.kind === 'ungrant' && (
        <ActionDialog
          title={t('members.ungrant.title', {
            name: memberName(t, open.member),
            wg: directory.workgroups.get(open.wg) ?? t('members.wg.unnamed'),
          })}
          testId="members.ungrant"
          confirmLabel={t('members.ungrant.confirm')}
          danger
          run={run(ungrantPlan(open.member.id, open.wg))}
          onDone={close}
          onClose={close}
        >
          <p>{open.member.role === 'admin' ? t('members.ungrant.admin-body') : t('members.remove.body')}</p>
        </ActionDialog>
      )}
      {open?.kind === 'name' && (
        <NameDialog org={org} subject={open.member.id} current={open.member.name ?? ''} onClose={close} />
      )}
      {open?.kind === 'grant' && (
        <GrantDialog org={org} member={open.member} state={state} directory={directory} onClose={close} />
      )}
      {open?.kind === 'leave' && (
        <ActionDialog
          title={t('members.leave.title')}
          testId="members.leave-dialog"
          confirmLabel={t('members.leave.confirm')}
          danger
          run={run(leavePlan())}
          onDone={() => {
            close();
            void app.loadMe().finally(() => navigate('/'));
          }}
          onClose={close}
        >
          <p>{t('members.leave.body')}</p>
        </ActionDialog>
      )}
    </main>
  );
}
