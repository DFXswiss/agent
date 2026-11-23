// Error element of the root route: an exception while rendering a screen shows the generic error page instead of
// React Router's developer message.
import { ErrorPage } from './ErrorPage';

/** Client-side code for an unexpected rendering error; not a hub code, so the generic catalog text is shown. */
export const UI_ERROR_CODE = 'ui_error';

export function RouteError() {
  return <ErrorPage code={UI_ERROR_CODE} />;
}
