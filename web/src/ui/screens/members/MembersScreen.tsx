// Members and rights (06-web.md section 7.1, route `/o/:org/members`, right org:member, edit: admin). The list comes
// from the verified log (display names from `member.profile`); admins see the controls – invite, change role (hidden for
// the last admin), grant workgroup access (optionally with history; every writable workgroup, the key holders deliver
// the key), ungrant an explicit access, remove – each change through the confirmation dialog ("Saving …",
// `submitBatch`). An access an admin has only by its role shows "reads as admin" and has nothing to ungrant (ADR-058).
// Every member can leave (hidden for the last admin). Controls the user's verified rights do not allow are not
// rendered (section 1.2).
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { type Planner, submitBatch, type SubmitProgress } from '../../../core/org/batch';
import {
  type Access,
  canManage,
  grantable,
  grantPlan,
  leavePlan,
  type MemberRow,
  memberRows,
  removePlan,
  rolePlan,
  ungrantPlan,
} from '../../../core/org/members';
import type { OrgState } from '../../../core/log/state';
import type { Directory } from '../../../core/tasks/directory';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { ActionDialog, BUTTON } from './ActionDialog';
import { AdmitSection } from './AdmitSection';
import { InviteSection } from './InviteSection';

/** The dialog that is open, with what it acts on. */
type Open =
  | { readonly kind: 'role'; readonly member: MemberRow }
  | { readonly kind: 'remove'; readonly member: MemberRow }
  | { readonly kind: 'ungrant'; readonly member: MemberRow; readonly wg: string }
  | { readonly kind: 'grant'; readonly member: MemberRow }
  | { readonly kind: 'leave' };

/** A member's display name, or a neutral label with the start of the ID. */
export function memberName(t: (key: string, params?: Record<string, string>) => string, row: MemberRow): string {
  return row.name ?? t('members.unnamed', { id: row.id.slice(-6) });
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

function MemberItem({
  row,
  admin,
  directory,
  onOpen,
}: {
  readonly row: MemberRow;
  readonly admin: boolean;
  readonly directory: Directory;
  readonly onOpen: (open: Open) => void;
}) {
  const { t } = useI18n();
  const manage = admin && !row.lastAdmin;
  return (
    <li className="flex flex-col gap-2 border-b border-gray-200 py-3" data-testid="members.item" data-member={row.id}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium" data-testid="members.item.name">
          {memberName(t, row)}
        </span>
        {row.isMe && <span className="text-sm text-gray-600">{t('members.me')}</span>}
        <span className="rounded bg-gray-100 px-2 text-sm" data-testid="members.item.role">
          {t(`members.role.${row.role}`)}
        </span>
      </div>
      <ul className="flex flex-wrap gap-2 text-sm" data-testid="members.item.access">
        {row.access.map((item) => (
          <li key={item.wg} className="flex items-center gap-1 rounded border border-gray-200 px-2">
            <span>{directory.workgroups.get(item.wg) ?? t('members.wg.unnamed')}</span>
            <span className="text-gray-600" data-inherited={item.inherited}>
              {item.inherited ? t('members.access.inherited') : t(`members.access.${item.access}`)}
            </span>
            {admin && !item.isDefault && !item.inherited && (
              <button
                type="button"
                className="min-h-11 min-w-11 underline"
                onClick={() => onOpen({ kind: 'ungrant', member: row, wg: item.wg })}
                aria-label={t('members.ungrant.label', {
                  name: memberName(t, row),
                  wg: directory.workgroups.get(item.wg) ?? t('members.wg.unnamed'),
                })}
                data-testid="members.item.ungrant"
              >
                ×
              </button>
            )}
          </li>
        ))}
      </ul>
      {admin && (
        <div className="flex flex-wrap gap-2">
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
    </li>
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
  const rows = memberRows(state, userId, directory);
  const me = rows.find((row) => row.isMe)!;
  const close = () => setOpen(null);
  const run = (planner: Planner) => (progress: (step: SubmitProgress) => void) =>
    submitBatch(app.tasks, org, planner, progress);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="members.screen">
      <h1 className="text-xl font-semibold">{t('members.title')}</h1>
      {admin && <AdmitSection org={org} state={state} directory={directory} />}
      {admin && <InviteSection org={org} state={state} directory={directory} />}
      <ul data-testid="members.list">
        {rows.map((row) => (
          <MemberItem key={row.id} row={row} admin={admin} directory={directory} onOpen={setOpen} />
        ))}
      </ul>
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
