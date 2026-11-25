// "Fix conflicts" (KOR-48, ADR-084 section 3): the fixed platform instruction (`instruction.ts`) posted as one ordinary
// prompt of the person who pressed it to the task the pull request is linked to – the chat's send code
// (`core/chat/actions.ts` `sendPrompt`: signed, sealed, kept for a retry after a lost answer). The task's row is loaded
// first when the browser does not hold it. Every follow-up continues the task's own conversation (ADR-072), so the AI
// that wrote the pull request resolves its conflicts with its full context.
import { sendPrompt } from '../chat/actions';
import type { TasksContext } from '../tasks/pull';
import { loadTask } from '../tasks/send';
import type { PullRef } from './address';
import { fixConflictsInstruction } from './instruction';

/** Sends the instruction for `ref` to `task`; returns the session the hub routed it to. */
export async function fixConflicts(context: TasksContext, task: string, ref: PullRef): Promise<string> {
  const row = context.store.getState().tasks.tasks[task] ?? (await loadTask(context, task));
  return sendPrompt(context, row, fixConflictsInstruction(ref));
}
