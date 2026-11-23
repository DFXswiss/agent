"""Shared helpers of the test doubles (docs/platform/spec/07-testing.md section 9); standard library only.

Every double follows the same conventions, so the E2E orchestrator and the tests treat them alike:

- `journal`: one record per request in a common JSON format, credentials redacted, optionally appended to a
  JSON-lines file.
- `state`: the document of ``GET /_double/state``; ``ok`` is true when every expectation was met and no unexpected
  request arrived.
- `server`: an HTTP server bound to exactly the given address, a base request handler with body limits, JSON and
  chunked responses.
- `cli`: the common command-line options (``--host``, ``--port``, ``--port-file``, ``--journal``) and the
  serve-until-signal loop.
- `client`: a small HTTP client for the control routes (``/_double/…``) and the double's own routes.
"""
