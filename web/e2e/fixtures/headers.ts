// Response headers of the hub (03-api.md section 1, 06-web.md section 10.1). The hub test server sends exactly these
// on every response, so the Playwright suite runs the build under the production CSP.
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "font-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join('; ');

export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  'Content-Security-Policy': CONTENT_SECURITY_POLICY,
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Permissions-Policy': 'publickey-credentials-get=(self), publickey-credentials-create=(self)',
};

export const CACHE_INDEX = 'no-cache';
export const CACHE_ASSETS = 'immutable, max-age=31536000';
