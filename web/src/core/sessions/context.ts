// How the current main session of a task got its context (ADR-072 sections 5 and 7, KOR-33): the hub reports on
// every `session_row` `context_restore` ("restored", "fallback" or null) and `context_reason`: with "fallback" why a new
// AI took over, with "restored" whether the workspace came back only partly (`workspace_partial`) or not at all
// because it was too large (`workspace_omitted`); the conversation itself is complete in both. Older hubs send neither
// field, so anything missing or malformed means "nothing to show". A fallback reason the client does not know is
// shown with the generic text, an unknown reason of a restore as a full restore.
import { isRecord } from '../registry';
import type { SessionEntry } from '../store/slices/tasks';

export const CONTEXT_REASONS = [
  'context_missing',
  'context_invalid',
  'context_unavailable',
  'version_incompatible',
  'restore_failed',
] as const;

export type ContextReason = (typeof CONTEXT_REASONS)[number] | 'unknown';

/** What came back of the workspace with a restored conversation. */
export type RestoredWorkspace = 'full' | 'partial' | 'omitted';

export type ContextResume =
  | { readonly kind: 'restored'; readonly workspace: RestoredWorkspace }
  | { readonly kind: 'fallback'; readonly reason: ContextReason };

const KNOWN: ReadonlySet<string> = new Set(CONTEXT_REASONS);
const WORKSPACE: ReadonlyMap<unknown, RestoredWorkspace> = new Map([
  ['workspace_partial', 'partial'],
  ['workspace_omitted', 'omitted'],
]);

/** The resume outcome of a session row, or null when the row reports none. */
export function contextResume(entry: SessionEntry | null | undefined): ContextResume | null {
  const raw: unknown = entry?.row;
  const row: Readonly<Record<string, unknown>> = isRecord(raw) ? raw : {};
  if (row.context_restore === 'restored')
    return { kind: 'restored', workspace: WORKSPACE.get(row.context_reason) ?? 'full' };
  if (row.context_restore !== 'fallback') return null;
  const reason =
    typeof row.context_reason === 'string' && KNOWN.has(row.context_reason) ? row.context_reason : 'unknown';
  return { kind: 'fallback', reason: reason as ContextReason };
}
