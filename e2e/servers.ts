// The two dashboards that playwright.config.ts starts with e2e/dashboard_server.py.
// Ports away from the default 7845, so a dashboard of a running daemon is never used.
export const FILLED_URL = 'http://127.0.0.1:17845';
export const EMPTY_URL = 'http://127.0.0.1:17846';

// The frozen server clock of e2e/dashboard_server.py (NOW); the browser clock is set to it.
export const NOW = '2026-11-17T10:00:00Z';
