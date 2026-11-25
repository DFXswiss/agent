// The fixed platform instruction "fix merge conflicts" (KOR-48, ADR-084 section 3): the text "Fix conflicts" posts as
// one ordinary prompt to the task a pull request is linked to. It is versioned; `agent_platform/pulls.py` produces
// exactly the same text (a test keeps both equal): `{repo}` is `<owner>/<name>`, `{number}` the decimal number, the
// lines joined with "\n" and no trailing newline.
import type { PullRef } from './address';

/** The version named in the text; a changed text is a new version. */
export const INSTRUCTION_VERSION = 1;

/** The instruction for the pull request `ref` (module comment). */
export function fixConflictsInstruction(ref: PullRef): string {
  const { repo, number } = ref;
  return [
    `Platform instruction "fix merge conflicts" (v${INSTRUCTION_VERSION}) for pull request ${repo}#${number}:`,
    `1. Call github.merge_base with {"repo": "${repo}", "number": ${number}}.`,
    '2. If the result says up_to_date, say so and stop.',
    '3. Resolve every listed conflict in the workspace directory of the repository, keeping the intent of both sides; remove every conflict marker; record a decision for every conflict without markers.',
    '4. If the repository has tests, run them with test.run on the resolved workspace and fix what the merge broke before you commit.',
    "5. Call github.commit with merge: true on the pull request's branch, then github.push of that commit to the same branch.",
    '6. Never merge, close or force-push the pull request.',
    '7. If a resolution needs a decision of a person, ask with human.question and wait for the answer.',
  ].join('\n');
}
