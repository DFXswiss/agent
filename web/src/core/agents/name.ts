// Names and handles of main agents (ADR-095 section 1, 06-web.md section 4.0a), vector-tested against
// `contracts/tasks/agent-name.json` (shared with the agent tool and the runner):
//
// - the automatic name of a task's main agent, derived from the task's current title: every run of Unicode whitespace
//   collapsed to one space and trimmed, the first `AUTO_WORDS` words, at most `AUTO_CODE_POINTS` code points (cut
//   before the last space among the first `AUTO_CODE_POINTS + 1` code points, or – without a space after the first one
//   – after `AUTO_CODE_POINTS` code points), `Agent` when nothing is left. Code points, not UTF-16 units, so an emoji
//   is one;
// - a name a person gives (`task.agent_name`, `contracts/events/objects.json#/$defs/body_task_agent_name`): 1–80 code
//   points on one line; the input is trimmed first;
// - the handles `#<number>` (the task's number in its organization, `task_row.number`) and `#<number>.<agent_path>`
//   (a subagent, `session_row.agent_path`); a value the hub sends malformed gives no handle.

/** Words of the title the automatic name keeps at most. */
export const AUTO_WORDS = 5;
/** Code points of the automatic name at most. */
export const AUTO_CODE_POINTS = 40;
/** The automatic name of a task without a usable title. */
export const AUTO_EMPTY = 'Agent';
/** Code points of a given name at most. */
export const NAME_MAX = 80;

const WHITESPACE_RUN = /\p{White_Space}+/gu;
const LINE_BREAK = /[\n\r\v\f\u0085\u2028\u2029]/u;
const AGENT_PATH = /^[1-9][0-9]{0,8}(\.[1-9][0-9]{0,8})*$/;

/** The automatic name of a main agent whose task has the title `title`. */
export function automaticName(title: string): string {
  const text = title.replace(WHITESPACE_RUN, ' ').replace(/^ | $/g, '');
  const name = text.split(' ').slice(0, AUTO_WORDS).join(' ');
  const points = [...name];
  if (points.length <= AUTO_CODE_POINTS) return name === '' ? AUTO_EMPTY : name;
  const head = points.slice(0, AUTO_CODE_POINTS + 1);
  const space = head.lastIndexOf(' ');
  return (space > 0 ? head.slice(0, space) : head.slice(0, AUTO_CODE_POINTS)).join('');
}

/** A name a person typed, trimmed; null when it is empty, longer than `NAME_MAX` code points or not one line. */
export function checkName(input: string): string | null {
  const name = input.replace(/^\p{White_Space}+|\p{White_Space}+$/gu, '');
  const length = [...name].length;
  return length === 0 || length > NAME_MAX || LINE_BREAK.test(name) ? null : name;
}

/** The task's number when the hub sent a well-formed one (`task_row.number`), else null. */
export function taskNumber(row: { readonly number?: unknown }): number | null {
  return Number.isSafeInteger(row.number) && (row.number as number) >= 1 ? (row.number as number) : null;
}

/** The session's place below its main agent when the hub sent a well-formed one (`session_row.agent_path`). */
export function agentPath(row: { readonly agent_path?: unknown }): string | null {
  return typeof row.agent_path === 'string' && AGENT_PATH.test(row.agent_path) ? row.agent_path : null;
}

/** `#<number>` of a main agent. */
export const mainHandle = (number: number): string => `#${number}`;

/** `#<number>.<path>` of a subagent. */
export const subagentHandle = (number: number, path: string): string => `#${number}.${path}`;
