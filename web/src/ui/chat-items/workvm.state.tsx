// Chat item `workvm.state` (13-work-vm.md sections 8 and 10, ADR-059 decision 10, ADR-080 sections 4 and 5): a work
// VM is starting (implicit start), was created, stopped (paused session) or deleted (with the reason), its size and
// resources and, once stopped or deleted, its runtime in h/min/s. A body that does not follow the contract is a
// neutral row with a code, never content.
import type { TimelineItem } from '../../core/chat/timeline';
import { formatRuntime, parseWorkvmState, type WorkvmState } from '../../core/chat/workvm';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';
import { Invalid } from './workvm.run';

/**
 * "Work VM created", "Work VM stopped (session paused)", "Work VM deleted (idle)", … A VM stopped because it was
 * unused (`stopped`, reason `idle`, ADR-059 addendum 4) reads "Work VM stopped, unused – starts again when needed".
 */
export function useStateTitle(): (vm: WorkvmState) => string {
  const { t } = useI18n();
  return (vm) => {
    if (vm.state === 'created' || vm.state === 'starting') return t(`workvm.state.${vm.state}`);
    if (vm.state === 'stopped' && vm.reason === 'idle') return t('workvm.state.stopped.idle');
    if (vm.reason === null) return t(`workvm.state.${vm.state}.plain`);
    return t(`workvm.state.${vm.state}`, { reason: t(`workvm.state.reason.${vm.reason}`) });
  };
}

function State({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t, locale } = useI18n();
  const stateTitle = useStateTitle();
  const vm = parseWorkvmState(body);
  if (vm === null) return <Invalid />;
  const gib = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  const title = stateTitle(vm);
  return (
    <p
      className="flex flex-wrap items-center justify-center gap-x-2 text-center text-xs text-gray-600"
      data-testid="chat.workvm.state"
      data-state={vm.state}
      data-workvm={vm.workvm_id}
    >
      <span className="font-medium" data-testid="chat.workvm.state.title">
        {title}
      </span>
      <span data-testid="chat.workvm.state.resources">
        {t('workvm.state.resources', {
          size: t(`workvm.state.size.${vm.size}`),
          cpus: vm.cpus,
          memory: gib.format(vm.memory_mib / 1024),
          disk: vm.disk_gib,
        })}
      </span>
      {(vm.state === 'deleted' || vm.state === 'stopped') && (
        <span data-testid="chat.workvm.state.runtime">
          {t('workvm.state.runtime', { runtime: formatRuntime(vm.runtime_ms) })}
        </span>
      )}
      <EventTime at={at} />
    </p>
  );
}

export const chatItem: ChatItem = { kind: 'workvm.state', Component: State };
