// Chat item `session.error` (ADR-064 section 5): an error the guest reported for the session, with the text of its
// code from the closed list of the guest journal (`contracts/guest/journal/record.json`); an unknown code is shown as
// such, never as free text.
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';

export const GUEST_ERROR_CODES: ReadonlySet<string> = new Set([
  'subagent_hook_seen',
  'gateway_route',
  'hook_handler_failed',
  'transcript_unreadable',
  'recorder_failed',
  'scanner_failed',
  'journal_write_failed',
  'pending_expired',
  'session_unit_failed',
  'credential_missing',
  'auth_failed',
  'auth_rate_limited',
  'turn_unconfirmed',
  'tools_unavailable',
]);
const CODE_RE = /^[a-z][a-z0-9_]{0,63}$/;

function GuestError({ item }: ChatItemProps) {
  const { at, body, source } = item as TimelineItem;
  const { t } = useI18n();
  const raw = isRecord(body) && typeof body.code === 'string' && CODE_RE.test(body.code) ? body.code : 'unknown';
  const text = GUEST_ERROR_CODES.has(raw) ? t(`guest-error.${raw}`) : t('guest-error.unknown', { code: raw });
  return (
    <p
      className="text-center text-xs text-amber-800"
      data-testid="chat.guest-error"
      data-code={raw}
      data-source={source}
    >
      {t('guest-error.prefix')} {text} <EventTime at={at} />
    </p>
  );
}

export const chatItem: ChatItem = { kind: 'session.error', Component: GuestError };
