// Deterministic task title (06-web.md section 4.2), vector-tested against `contracts/tasks/title.json` (shared with
// the runner's `task.set_title` check): the first line of the prompt that contains a non-whitespace character, with
// every run of Unicode whitespace collapsed to one space and trimmed; longer than 80 code points → the first 79 code
// points + `…`. A prompt without such a line has the empty title.
//
// Definitions (binding for every implementation, see the vector file): whitespace = the Unicode `White_Space`
// property; line breaks = `\r\n`, `\n`, `\r`, U+000B, U+000C, U+0085, U+2028, U+2029.

export const TITLE_MAX_CODE_POINTS = 80;
export const ELLIPSIS = '…';
const LINE_BREAK = /\r\n|[\n\r\v\f\u0085\u2028\u2029]/u;
const WHITESPACE_RUN = /\p{White_Space}+/gu;

/** The title of a prompt. */
export function deriveTitle(prompt: string): string {
  for (const line of prompt.split(LINE_BREAK)) {
    const title = line.replace(WHITESPACE_RUN, ' ').replace(/^ | $/g, '');
    if (title !== '') {
      const points = [...title];
      return points.length > TITLE_MAX_CODE_POINTS
        ? points.slice(0, TITLE_MAX_CODE_POINTS - 1).join('') + ELLIPSIS
        : title;
    }
  }
  return '';
}
