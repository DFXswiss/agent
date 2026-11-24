// The closed set of windowed lists of the web client (ADR-063 sections 1 and 4): every `WindowedList` names one of
// these ids, and the volume gate (`e2e/volume`) must measure each of them. A new list adds its id here.
export const LIST_IDS = [
  'task.chat',
  'session.chat',
  'chat.files',
  'task.operations',
  'task.sessions',
  'task.audit',
  'whiteboard.entries',
  'whiteboard.history',
  'notifications',
  'audit',
  'tasks.list',
  'tasks.overview',
  'members',
  'members.invites',
  'devices',
  'devices.invites',
  'workgroups',
  'platform-invites',
  'settings.agents.activity',
  'monitoring.tree',
] as const;

export type ListId = (typeof LIST_IDS)[number];

export function isListId(value: unknown): value is ListId {
  return typeof value === 'string' && (LIST_IDS as readonly string[]).includes(value);
}
