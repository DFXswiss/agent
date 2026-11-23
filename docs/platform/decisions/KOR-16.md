# KOR-16 decisions (sporadic `upstream_unreachable` at "Connect GitHub" on the two-device topology)

Observed twice on T2 (never on T1): the runner logged `github_connect failed code=upstream_unreachable`, the GitHub
double journaled `POST /login/device/code` with status 200, and the test relay `github` on the runner device recorded
that connection as closed about 4 ms after it opened, with request bytes forwarded and 0 bytes back. In one lane the
two following connects (E2E-32, E2E-33) failed; every other lane connected normally. The artefacts of both runs were
overwritten before this package, so the analysis rests on the recorded picture, the code and tests.

1. **Explanations checked.**

   | Explanation | Result |
   |---|---|
   | Runner aborts itself: a deadline-derived timeout | excluded: the GitHub client's timeout is a fixed 30 s per phase, the flow's deadline (`asyncio.wait_for`) ends with `timeout`, not `upstream_unreachable` |
   | Runner aborts itself: cancelled flow (request processed twice, replaced, not listed) | excluded: requests are processed by one queue consumer and a running request is skipped; a cancelled flow raises `CancelledError` inside httpx (checked), which logs no `github_connect failed` |
   | Runner closes its side first (any reason) | **not excluded by the record**: the relay counted bytes down only after a successful send to the client, so "the client side was gone when the answer came" also gives 0 bytes down; no mechanism in the runner was found that closes the fresh connection within milliseconds and logs `upstream_unreachable` (a cancelled flow logs nothing, a failed read needs an answer or a reset) |
   | Reused or half-closed keep-alive connection | excluded: the connect uses a fresh client (own pool); the device-code connection is the first of that client; httpx never half-closes |
   | Name resolution, proxy, egress rules | excluded: the URL is a loopback address literal, the client ignores proxy variables (`trust_env=False`), the egress sampler only reads (`ps`, `lsof`) |
   | Double loses the answer | excluded: the double journals status 200 only from `_send`, i.e. it wrote (or tried to write) the answer to the relay's upstream socket; it never closes a connection before answering except for an armed `disconnect` fault (status 0) |
   | Descriptor limit (`EMFILE`, soft limit 256 on the devices) kills the relay's accept loop | excluded: at most 7 relayed connections are open at once on the runner device (all saved T2 egress logs) |
   | Two listeners on one port (lane ports are reused) | excluded: a second bind of `127.0.0.1:<port>` is refused (`EADDRINUSE`) |
   | Relay stalled ≥ 30 s before forwarding (the runner's read timeout, then the late forward finds the client gone) | not supported: request-to-open delay 24–279 ms in all 84 saved T2 connects; a stall of the whole node would also stall `runner-hub` |
   | **Relay closes a socket twice from two threads** | **most plausible candidate, not reproduced** (point 2) |

2. **Candidate: the relay released descriptors from its copy threads (latent defect, fixed; not proven to be the
   cause).** Each relayed connection has two copy threads; the first one to end shut down *and closed* both sockets,
   and the second did the same once it woke up. CPython reads a socket's descriptor number for `shutdown`, `recv` and
   `send` after it has released the interpreter lock (`sock_close` sets it to -1 under the lock, then closes). If the
   second thread was pre-empted between reading that number and the system call while the first closed the socket,
   the number could be handed to a connection accepted meanwhile by any relay of the same process; the late
   `shutdown` then ended that connection. For a device-code request this gives the recorded picture (request
   forwarded, the double answers 200, the relay records open → close within milliseconds and 0 bytes down, the runner
   reads end-of-file: `RemoteProtocolError`, kind `unreachable`;
   `test_a_late_shutdown_on_a_reused_descriptor_gives_the_recorded_picture` establishes this situation). **Not
   explained by it:** two connects failing back to back in one lane (the window is a few instructions wide) and that
   it was never seen on T1 (there the same race exists with more relays in one process). It was not reproduced: 0 of
   3000-request loops through relay and double with concurrent relay churn and CPU overload, 0 of 20 T2 iterations of
   E2E-20/-21/-32/-33 (1200 relayed GitHub connections, every one ended `up`/`eof` with an answer). The cause
   therefore stays **undetermined**; the defect is corrected because it can produce exactly this failure, and the
   diagnosis below makes the next occurrence decisive (`first`/`reason` of the relay tell which side ended the
   connection; `cause` of the runner tells end-of-file, reset or timeout).
3. **Correction (test environment).** A socket of a relayed connection is closed exactly once, by the connection's
   thread after both copies ended; copies and `cut()` only shut sockets down (`_wake`), and `cut()` does so under the
   lock the connection's thread takes before closing. While a descriptor can still be used it cannot be reused.
   Tests: `test_a_copy_wakes_its_sibling_but_never_releases_a_descriptor` (red before: the copy closed both sockets)
   and `test_every_relayed_connection_releases_its_descriptors_once_it_ended` (no descriptor leak, also after a cut).
   A tap file that cannot be written ends its copy with `tap:<error>` (the old code swallowed it; covered by
   `test_a_tap_that_cannot_be_written_ends_the_connection_with_its_error`). No scenario retries, no timeout raised,
   no assertion weakened. Left as they were (existing design, not part of the failure): a connection whose upstream
   connect is still running when `cut()` is called is not in `_open` yet and survives the cut; half-close is not
   passed through (end-of-file in one direction ends both).
4. **Diagnosis kept.** The relay's end record names the direction that stopped first and why (`first`: `up`/`down`,
   `reason`: `eof`, `recv:<error>`, `send:<error>`); the runner logs the step, kind, status and transport-error class
   of a failed GitHub call (agent repository, decisions KOR-16 there).
5. **Product: the first device-flow call is retried** (agent repository, decisions KOR-16 point 2; `05-runner.md`
   section 11.4 in both repositories).

## Addendum (KOR-18, 2026-11-23): cause determined

The sporadic `upstream_unreachable` of the GitHub connect on the two-device topology was the keep-alive race between
the runner's HTTP client and the hub: both used a 5 s idle limit, so a request sent just before the hub closed an idle
connection was lost. The failing line came from the credential store at the end of the flow (the only place that
produced the code without `step=`). Corrected and evidenced in KOR-18 (decisions KOR-18).
