# KOR-16 decisions (agent repository: device-flow robustness and diagnosis)

The cause of the sporadic `upstream_unreachable` at "Connect GitHub" on T2 is not determined (agent-core
`docs/platform/decisions/KOR-16.md`: explanations checked; the most plausible candidate, a socket closed twice from
two threads in the E2E test relay, is fixed there but not reproduced). Independent of the cause:

1. **Diagnosis.** `GitHubError` carries `cause`, the class name of the transport error of an `unreachable` failure
   (`ConnectError`, `ReadError`, `RemoteProtocolError`, `ReadTimeout`, ...: it names the phase and holds no secret).
   `github_connect failed` and the new `github_connect retry` lines name the step (`device_code`, `poll`, `user`),
   the kind, the HTTP status and the cause. Before, the line only said `code=upstream_unreachable`, which could not
   tell a refused connect from a lost answer.
2. **The first call of the device flow is retried (implemented).** `POST /login/device/code` is retried after a
   network error or a `5xx` (kinds `unreachable`, `server_error`) after 2 s and 8 s - three attempts, the first two
   waits of the operation pipeline's backoff (03-api.md section 9: 2 s, 8 s, 30 s); then `upstream_unreachable` as
   before. Reasons: the request has no effect for the user (an unused device code expires at GitHub), the polling
   already tolerates five network errors in a row, and one lost connection on a real network must not end a connect
   with "service could not be reached".
   Not retried: `rate_limited` (GitHub names a wait of up to an hour; the user sees the failure and may start again)
   and every non-transient answer. Bounds: at most 10 s of waiting plus three 30 s request timeouts, inside the
   request's 15 minutes (the flow's deadline still ends everything with `timeout`). Tests:
   `test_a_device_code_request_whose_connection_drops_once_is_sent_again` (the double's `disconnect` fault: journal
   has the request, the client reads nothing - the recorded picture; red before),
   `test_a_device_code_request_that_keeps_failing_ends_upstream_unreachable`,
   `test_a_rate_limited_device_code_request_is_not_sent_again`.
3. `05-runner.md` section 11.4 names the retry (identical in both repositories); no contract change.

## Addendum (KOR-18, 2026-11-23): cause determined

The sporadic `upstream_unreachable` of the GitHub connect on the two-device topology was the keep-alive race between
the runner's HTTP client and the hub: both used a 5 s idle limit, so a request sent just before the hub closed an idle
connection was lost. The failing line came from the credential store at the end of the flow (the only place that
produced the code without `step=`). Corrected and evidenced in KOR-18 (decisions KOR-18).
