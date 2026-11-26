# ADR-101: Remote control by VNC when a process needs a human – a real RFB connection to the sandbox's screen, only for the owner's own identity, tunnelled end to end through the hub, opened by the agent tool without a link

Status: Accepted (2026-11-26)

## Context
- The client (2026-11-26): "ich will keinen link, ich will dass du alles erledigst und wenn ich eingreifen muss dann
  gibst du mir kurz vnc"; earlier: "bei so einer situation bitte vnc öffnene, dann mach ich das kurz". Occasion: when
  new Claude accounts are registered, claude.ai shows a check for humans. It is never circumvented; the person solves
  it. The rule on remote control at such checks is E-39 of the concept (branch of the concept text, not repeated here).
- Built before: the check for humans of a login, paused and handed to the owner as a sealed picture with clicks carried
  as `{click: {x, y}}` (only the owner's own identity, `VERIFY_WAIT` 180 s; [ADR-099](099-parallel-subscription-logins.md)
  section 4, KOR-73); the platform browser in its own browser VM ([ADR-085](085-browser-as-platform-operation.md)); the
  work VM ([ADR-059](059-work-vm-on-request.md)); device requests sealed end to end between the user and one device
  (03-api.md section 4.10, 04-crypto.md section 4.5); the rights check and the audit.
- A picture with clicks is not enough for every check: some need a drag, a key, a second click while the page moves,
  or simply a person who sees the page live. A remote screen is what the client asked for.

## Decision

### 1. What is offered, and to whom
- Every sandbox with a screen offers a real VNC connection (RFB) **while a process in it waits for a human**, and only
  to the **owner of that process**:
  - the check for humans of a login (ADR-099 section 4) – owner: the login's user;
  - the platform browser of a session (ADR-085) – owner: the task owner (`credential_owner` of the session on the
    device); the browser is offered while it is open.
- Only the owner's **own identity** may take a screen over (the web's passkey browser, or the agent tool signing as the
  user), **never an agent**: the device answers an agent's `vnc.open` with `failed forbidden` (log line
  `runner.vnc refused reason=agent`), and the hub closes the client end of a tunnel whose request was not signed by
  the user. Limit (as in ADR-099): the device cannot tell a person from a program that holds the user's own key; the
  tool says in its help and errors that a person works on the screen.

### 2. The screen
- The platform browser helper runs Chromium **with a window** on a virtual display (`Xvfb :99`, the size of the
  viewport) when Xvfb is installed, headless otherwise. The browser looks to a page like a normal desktop browser
  without any disguise: nothing is changed to pass a check (ADR-099 section 4 stays: no solver, no stealth).
- The work image installs `xvfb` and `x11vnc` and the helper `workvm-vnc`: `start` reads a one-time password
  (8 letters or digits; classic VNC authentication uses at most eight characters) on stdin and serves the display on
  the VM's **loopback** only for exactly that password; `connect` joins stdin and stdout with that port; `stop` ends
  the server and removes the password file. The runner calls it with `sudo -n` over the slot's `limactl shell`, like
  `workvm-browser`. Login VMs and browser VMs use the same image.
- A login sandbox without a VM (`LOGIN_SANDBOX=process`) has no screen: `needs_human` says `vnc_available: false`
  and the picture with clicks of ADR-099 stays the way.

### 3. The connection
- **Request** `vnc.open {kind: "login"|"session", id}` (a new device-request kind, sealed to the device, `slot` = `id`,
  so the owner holds one connection per screen; no follow-ups). The device checks the requester (section 1), finds
  the screen, starts its VNC server with a fresh one-time password, draws a fresh 32-byte tunnel key and answers
  `{step: "vnc", key, password, idle_seconds}` (sealed to the requester like every response; never logged). No screen
  of the owner under that target: `failed no_screen`.
- **Tunnel through the hub over the existing signed connection**: both ends open a WebSocket at the hub, signed like
  the device WebSocket – the client `GET /v1/vnc/{request}` (right `owner`; the request must be an active `vnc.open`
  root of the user signed by the user), the device `GET /v1/device/vnc/{request}` (right `device_addressed`). The hub
  pairs them (at most 120 s), sends each `{"type":"paired"}` and then copies binary records unchanged until one end
  leaves or the request expires. The pairing lives in the memory of the hub process (the hub runs as one process).
- **End to end encrypted**: each direction has its own key `HKDF-SHA256(key, salt = request ID, "agent-vnc-v1\0" ‖
  c2d|d2c)`; a record is `AES-256-GCM` of a chunk of at most 16 KiB of the RFB stream with the record's number as
  nonce, so the hub can neither read nor reorder, drop or repeat records unnoticed; an empty chunk ends a direction.
  The hub sees that a tunnel exists, when, how long and how many bytes – never the screen, the keys typed or the
  password. The VNC password is a second lock inside the encrypted stream.
- **On the device** the tunnel's records are joined with `workvm-vnc connect` once the client joined. The connection
  ends – and the VNC server is stopped on every path – when:
  - the process goes on or ends (`ended`): the check passed or was given up, the login ended, the browser session
    closed or lost its VM;
  - nothing came from the viewer for `idle_seconds` (120 s; a VNC viewer sends nothing while nobody works on a screen
    that does not change: `idle`);
  - the viewer or the server closed it (`closed`), no client joined in time (`unpaired`), the request's 15 minutes
    ran out (`timeout`), a record did not open (`broken`), the hub was unreachable (`unreachable`).
  The final response `{step: "done", reason}` names it.

### 4. The process waits, the platform says so, the owner's control session opens it
- A login whose root says `vnc: true` (a client that takes screens over; sent only to a device that announced the
  feature `vnc`) answers `{step: "needs_human", vnc_available}` when its check for humans starts – the event "a process
  waits for a human" – and offers its screen until the check is gone or given up.
- While a VNC connection to the check is open, the person works on the screen itself: the check sends no pictures and
  keeps waiting (`VERIFY_WAIT` starts again at every look), so the person is never cut off while working. When the
  page is no check any more, the login continues by itself (`progress verified`) and the connection ends (`ended`).
- **Agent tool** (`agent-platform`, its own small WebSocket client `agent_platform.wsclient` on the standard library,
  so the tool still reaches only `httpx` and `cryptography`): `vnc --login <request> | --session <id> | --task <id>`
  sends `vnc.open`, listens
  on `127.0.0.1` only (a free port), starts the viewer itself – on macOS the built-in Screen Sharing with
  `open vnc://:<password>@127.0.0.1:<port>`, elsewhere `vncviewer` when installed (else port and password on stderr
  for the person) – and joins the first local connection with the tunnel; no further local connection is accepted,
  and the command returns when the connection ended. No link to click. `subscriptions add|login --vnc` puts
  `vnc: true` into the login and, when `needs_human` arrives with `vnc_available`, opens the connection on its own and
  then follows the login again: the owner's control session opens VNC by itself when a human is needed.

### 5. Audit (metadata only)
- `device_request.*` as for every device request (kind `vnc.open`).
- `vnc.connect` when both ends joined and `vnc.close {reason, seconds, bytes}` when the tunnel ends, actor the user,
  target the request, the device named. Nothing of the screen or the input.

### 6. Feature and older parts
- Feature `vnc` (ADR-060 decision 6): a runner that answers `vnc.open`, understands the root field `vnc` and sends
  `needs_human` announces it; clients send them only to such a device. An older runner answers `vnc.open` like every
  unknown kind (`failed upstream_error`); an older client never sends `vnc` and never sees `needs_human`.
- Roll-out: hub first (migration `0041_vnc_open`: the request kind), then runners and the work image, then the agent
  tool.

### 7. Tests
- A VNC double (`tests/platform/runner/vnc/double.py`; the doubles package stays standard-library only): an RFB 3.8
  server with classic VNC authentication whose clicks are clicked into the real page of the login's sandbox, and a
  viewer double that authenticates and clicks. The helper `workvm-vnc` is tested against a fake server program.
- The tunnel (directions, order, forged and repeated records, idle), the hub relay (pairing, one end per side,
  records unchanged, limits, audit without content), the routes (only the owner's own identity and the addressed
  device; an agent's request and a foreign user are refused), the handler (agent refused, no screen, start failure,
  every end reason), the flow check for humans → VNC → the login continues, and the agent tool (loopback only, viewer
  started, `--vnc` opens by itself, an agent is refused).

## Consequences
- The hub learns that a person took a screen over, when, for how long and how many bytes flowed; never what was on it.
- The login VM and the browser VM run a display and a VNC server only while a connection is offered; the server
  listens on the VM's loopback and is reached only through the runner's `limactl shell`.
- Chromium with a window uses a little more memory than headless (the virtual display).
- Deviation from ADR-085 section 6 ("the live view has no input path"): the owner's own identity has an input path to
  the browser VM **while it takes it over by VNC**; the action record of ADR-085 does not record what the person does
  on the screen (it is the person's work, not the session's).

## Alternatives considered
- A link to a web VNC page (noVNC): the client said "kein link".
- Opening a port of the device or the VM to the internet: no new open port; the signed WebSocket at the hub is the
  existing way between client and device.
- Relaying through device-request responses: at most 100 responses per request and polling – far too slow for a
  screen.
- Plain RFB through the hub with only the VNC password: the hub would see the screen and the typed keys (ADR-003).

## Deviation from DESIGN.md
None beyond [ADR-085](085-browser-as-platform-operation.md).
