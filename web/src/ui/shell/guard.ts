// Which routes need an unlocked identity (06-web.md section 2.4: every route with a right other than `anon`), and the
// sign-in target that brings the user back. `next` only ever names a path of this origin.
const PROTECTED = [/^\/o\//, /^\/settings(\/|$)/, /^\/subscriptions(\/|$)/, /^\/notifications(\/|$)/, /^\/platform\//];

export function needsUnlock(pathname: string): boolean {
  return PROTECTED.some((pattern) => pattern.test(pathname));
}

/** `/login?next=<path>` for the current location. */
export function loginTarget(pathname: string, search: string): string {
  return `/login?next=${encodeURIComponent(pathname + search)}`;
}

/** The `next` of a sign-in if it is a local path (`/x`, never `//host` or a URL), else null. */
export function safeNext(value: string | null): string | null {
  return value !== null && /^\/(?![/\\])/.test(value) ? value : null;
}
