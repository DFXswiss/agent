# ADR-099: Subscriptions logged in side by side – an own mailbox per subscription, every login in its own browser sandbox, a check for humans done by the owner, registration of a new account

Status: Accepted (2026-11-26)

## Context
- Rule [E-33](../KONZEPT.md#e-33) (the client, 2026-11-26): platform operations run side by side, each in its own
  sandbox, no single slots; every subscription has its own mailbox. To five new subscriptions: "die 5 personen haben
  noch kein abo, also man muss nicht nur anmelden sondern auch registrieren, und
  ich will dass du alles erledigst". Also [E-31](../KONZEPT.md#e-31) (every subscription logged in centrally, measured,
  used where the owner switches it on), [E-04](../KONZEPT.md#e-04)/[E-27](../KONZEPT.md#e-27) (everything without a
  browser too), [E-25](../KONZEPT.md#e-25) (no protection layers nobody ordered).
- Built before: further subscriptions as `claude_subscription` records ([ADR-090](090-every-subscription-logged-in-centrally.md))
  with the full login and renewal of [ADR-092](092-full-subscription-login-and-renewal-on-the-device.md); the mailbox
  automation of 05-runner §11.3 ([ADR-011](011-claude-subscription-login.md)); the platform browser, one VM per browser
  session ([ADR-085](085-browser-as-platform-operation.md)).
- Found (code and live, KOR-73):
  1. one `mailbox` credential per user: five subscriptions with five mailboxes can only be logged in one after the
     other, swapping the mailbox, and a later new login works automatically only for the last one;
  2. the automation recognises pages by the markers of the auth-page double (`body[data-page=…]`); against the real
     pages it gives up at the first step;
  3. the real sign-in page answers an automated browser without a window with a check for humans (Cloudflare
     Turnstile, "Sicherheitsüberprüfung wird durchgeführt");
  4. a new account (name, date of birth, terms) cannot be created;
  5. the hub keeps at most one active `claude.login` per user and device, the runner one flow per user and kind: a
     second login cancels the first.

## Decision

### 1. An own mailbox per subscription
- The sealed body of a further subscription gains `mailbox` (the body of `credentials/mailbox.json`: IMAP host, port,
  user, password, address). It is written by the owner's client like name, note and use (sealed under the owner's
  `CK`, never seen by the hub) – so a user holds as many mailboxes as subscriptions (at most `MAX_SUBSCRIPTIONS`).
- **Adding with a mailbox** creates the record first: the client writes the new record without a token (`kind`
  `oauth_login`, `email` = the address to sign in with, `name`, `note`, `mailbox`, `use`) with `PUT
  /v1/subscriptions/{id}` (`expected_version` 0, the plaintext mirror of `use`), then sends `claude.login`
  `{mode:"automatic", subscription_id}`. The device reads the record and signs in with its mailbox; the login keeps
  `mailbox` like name, note and use (runner `KEPT`), and so does every renewal (it changes only the token fields).
- **Order of the mailbox** of an automatic login: the record's `mailbox`, else the user's single `mailbox` credential
  (transition: records and the single mailbox from before stay valid; a record without `mailbox` behaves as before).
  The address: the request's `email`, else the record's `email`, else the mailbox's own.
- A **new login** of the subscription (`subscriptions login --id … --automatic`, "Jetzt anmelden") reads the same
  record and therefore its own mailbox – for every subscription, not only the last one.
- The mailbox of an existing subscription is set or changed by the owner's client (`subscriptions mailbox`, the
  subscription card in the web) as a new record version; `mailbox.check {subscription_id}` checks the own mailbox of
  that subscription (without it the single mailbox, as before).
- Who opens the mailbox password: whoever opens the record – the owner's clients and the devices eligible for the
  owner's `CK` – exactly as for the token in the same body; it never reaches a guest, a log or the hub.
- A runner from before keeps only name, note and use on a new login (its `KEPT`) and would drop `mailbox`: clients
  send the login of a record with `mailbox` only to a device that announced `subscriptions.parallel` (§7).

### 2. Logins side by side
- **Hub:** `POST /v1/device-requests` takes an optional plaintext `slot` (a UUID; for `claude.login` the
  subscription record it fills – the hub sees record IDs anyway). An active root is replaced only by a new root of
  the same user, device, kind **and slot** (`device_request.slot`, hub migration); roots without slot behave as
  before (the primary record's login stays one at a time; a root without slot never replaces one with a slot and vice
  versa). The hub sets no further limit.
- **Shared mailboxes** (aliases of one IMAP account): the sign-in mail is matched by sender and time only, so logins
  that share a mailbox take turns from entering the address to reading the mail (one lock per mailbox on the device);
  logins with different mailboxes are not held up.
- **Runner:** at most one flow per owner, kind and slot is active, and at most `MAX_SLOTS` (40, `MAX_SUBSCRIPTIONS`)
  slots per owner and kind (a further root is answered `busy`, so one user cannot queue unbounded logins on a
  device); the slot of `claude.login` is the
  `subscription_id` of the **decrypted** body, never the hub's plaintext (a hub that lies about the slot can only make
  the owner's own logins replace or not replace each other, which it can do anyway by not delivering).
- Every login has its own working directory (`tmp/login-<request>`, PTY, `HOME`, `CLAUDE_CONFIG_DIR`; 05-runner
  §11.2) and its own browser sandbox (§3). Nothing of one login is visible to another.
- **Adding several subscriptions** is one action for the owner: the client sends one `claude.login` per entry at
  once (each with its own record and slot) and shows the progress of each from that request's responses – in the web
  ("Mehrere Abos hinzufügen") and with the agent tool (`subscriptions add-many --file`), E-04. The limit is the
  device's capacity (§3), never a fixed number.

### 3. Every login in its own sandbox
- `LOGIN_SANDBOX` (runner setting, `vm` default, `process`):
  - **`vm`** – a **login VM**: a `runner.workvm` row of the new scope `login` (runner migration), one per login,
    obtained like a browser VM of ADR-085 §2 (warm pool, FIFO capacity queue, slot, RAM, disk, the outside boundary
    of the work VMs) and driven through the same helper (`workvm-browser call`: navigate, evaluate, type, click –
    also at a viewport point –, every answer with the page's picture). It belongs to no session, task or organization:
    no per-organization or per-owner limit applies (the device's capacity does), it is not billed (a login is the
    device's own work for the owner and lasts minutes) and no `workvm.state` event is written. It is deleted on
    every path of the login (`login_ended`, shielded from cancellation); a runner restart deletes it like every VM
    (`restart`). The login's request ID is its `session_id` column.
  - **`process`** – a headless Chromium of the device (the `login` extra) with its own profile directory below the
    login's working directory, one process per login, removed with that directory. For devices without work VMs and
    for the hermetic end-to-end topology, whose doubles a VM cannot reach (the VM's boundary). Only when set
    explicitly: it is a separate browser process, not a VM (a deviation from the package's "one VM per browser
    session", accepted for devices without work VMs and the hermetic tests). A device with `vm` but without work VMs
    gives the automation up (`no_sandbox`, the relay).
- Both sandboxes answer the same four calls (`goto`, `evaluate`, `type`, `click` at a target or a point) with the
  page's URL and, on request, its picture (JPEG, at most 100 KiB, the ladder of ADR-085 §4). The automation (§4) is
  one code path over both.

### 4. The real pages, honestly
- Pages are recognised by their structure and address, not by the double's markers: one script evaluated in the page
  classifies it as `challenge` (a Cloudflare challenge or Turnstile frame, an hCaptcha or reCAPTCHA frame, the
  challenge titles), `email` (an e-mail input and a continue button), `check_email` (the "check your e-mail" state;
  with a code input when the provider offers the code), `register` (inputs for the full name and/or the date of birth
  or consent boxes of a new account), `authorize` (the OAuth authorize page with its allow button), `code` (the code
  page of the callback), `error` or `unknown`, and marks the elements to act on (`data-agent-login`). The double
  (DBL-03) is extended to the real shapes (e-mail page with a "Continue with email" button, a code input on the
  check-email page, the registration page, a page with a check for humans) and the automation never reads its
  markers.
- The sign-in mail: its link (`<origin>/magic-link#…`) is opened in the sandbox; without a link, the code of the mail
  (six digits after "code") is typed into the code input. INBOX and the spam folder, the sender filter and the
  limits of 05-runner §11.3 stay.
- **A check for humans is never circumvented**: no disguise of the automated browser (no change of the user agent,
  of `navigator.webdriver`, of the automation flags), no solver, no third-party service, no repetition to wear the
  check down. When the classification says `challenge`, the login **pauses** and hands the check to its owner:
  - the device answers `verify {frame, image}` – the sandbox's picture, sealed to the requester like every response
    (the hub sees neither picture nor address) – and keeps the picture live: every `VERIFY_TICK` (2 s) it looks at the
    sandbox and sends a new picture when it changed, and after each click at once when the page settled; at most
    `VERIFY_FRAMES` (80) pictures per login, so a login stays within the hub's 100 responses per request (the rest is
    progress and the end). When the pictures are used up, the automation gives up (`human_check`). This is the live
    picture of the login, not the frame route of ADR-085 §6: a login has no workgroup and no session, and the
    responses are already sealed to the owner;
  - the owner clicks into the picture; the client sends the follow-up `{click: {x, y}}` (fractions of the picture)
    and the device clicks that point of the sandbox's viewport – the owner does the check, the platform only carries
    the click (like a remote screen). **Only the user's own identity may click** (the passkey browser, or the agent
    tool signing as the user): a click of the user's agent (ADR-082 own identity) is ignored and logged
    (`click_refused`) and gives the check no further time; `{relay: true}` may come from either. Limit: the device
    cannot tell a person from a program that holds the user's own key; the agent tool refuses `--click` for an
    own-identity agent and says in its answer that a person clicks, never a model – a rule for the tool's users,
    not something the platform can prove;
  - when the check is gone, the login continues (`progress verified`); `{relay: true}` from the owner, or no click
    within `VERIFY_WAIT` (180 s), gives the automation up: the device answers `authorize` with the link
    (`automation: "failed"`, `reason: "human_check"`) and the login continues as the relay in the owner's own
    browser, exactly as before.
- Without the owner nothing waits longer than `VERIFY_WAIT`; every other unexpected state falls back to the relay at
  once with its `reason` (`unknown_page`, `mail_timeout`, `browser_failed` – also a missing extra or a code of the
  wrong form –, `no_sandbox`, `disabled`).
- **Older clients**: the web parser and the agent tool from before accept only the known steps, stages and fields. A
  client that understands `verify`, `reason` and the new stages says so with `extended: true` in the root body; to
  any other request the device sends none of them (the new stages are left out, `reason` is omitted) and a check for
  humans falls back to the relay at once.
- **What is possible without circumvention and what is not:**
  - possible: the e-mail, link, code, registration and authorize pages are driven; a check that a human passes with a
    click in the picture passes, because the click is a human's;
  - not possible: a check that refuses the automated browser itself (Turnstile also scores the browser, not only the
    click) stays unsolved by any click – then the login ends in the relay after `VERIFY_WAIT` or the owner's `relay`,
    and the owner signs in in the own browser (link and code; the device still creates the token). How often the
    provider asks, and whether a click passes, depends on the provider and on the sandbox's network; it is measured in
    operation, not promised here. The suite has no live test against the provider (PROJECT.md).

### 5. Registration
- `claude.login` (automatic) takes `account {name, birth_date?, consents?}` – data of a new account, used only when
  the provider asks for it on the `register` page, never stored, never logged. `consents` lists only what was given
  explicitly: `terms` (terms of use and privacy policy), `age` (the minimum age), `marketing` (product e-mails).
- Consents in a request of the user's agent (own identity) are dropped (`consents_ignored`): only the user declares
  them. A box is ticked exactly when its consent was given – a pre-ticked box without consent is unticked.
- **Whose consent:** the account belongs to the person whose address it is, who may not be the platform user. The
  clients ask for each consent with the statement that the account holder gave it (unticked by default), and the
  signed request is the record of who declared it and when; the platform user acts as that person's agent and is
  responsible for having the consent, as with every account created for someone else. Whether the provider allows
  accounts to be created for another person is the provider's terms; the platform neither checks nor promises it.
- On the `register` page the automation fills the name and the date of birth (a date input, or day, month and year
  inputs) and ticks **only** the boxes whose consent the owner gave (classified by their label text). A box it must
  tick for the page to continue but cannot classify, or whose consent is missing, ends the automation
  (`consent_missing`); a registration without `account` ends it with `account_missing`. Both fall back to the relay
  (the owner registers in the own browser). After the registration the login continues (`progress registered`); a
  check for humans there is handled as in §4.
- No payment data, ever. A **team seat** (invitation, premium seat) is not part of this: the team's admin invites the
  address in the provider's admin settings (Members → invite, the seat type chosen there); the invitation reaches
  the subscription's mailbox and is accepted with that account; until then the account has no plan and the
  measurement shows no windows or "not readable". After the seat is given, "log in again" fetches a token with the
  plan.

### 6. Use and measurement
- A new subscription is measured as every logged-in further subscription (ADR-090 §4, ADR-092 §3). The organizations
  that may use it are named at adding time (`use.orgs` of the record written before the login, with the mirror, E-31);
  without them it is measured only.

### 7. Feature, older clients and older runners
- **Feature `subscriptions.parallel`** (ADR-060 decision 6): a runner that keys logins by slot, reads the record's
  mailbox, understands `account`, `extended`, `click`, `relay` and `mailbox.check {subscription_id}` announces it.
  Clients send `slot`, `account`, `extended`, a record with `mailbox` (also its new login) and several logins at once
  only to a device that announced it; with an older device they keep the single login of ADR-090 (one at a time, the
  single mailbox).

### 8. Tests
- The auth-page double (DBL-03) gains the real shapes (§4), a code in the sign-in mail, a registration page for new
  addresses and a page with a check for humans whose box passes only when clicked at its place.
- Runner: five logins at once with five records and five mailboxes in one run, each in its own sandbox (the real
  browser helper, one per login); a new login of a subscription from its own mailbox; registration with and without
  the needed consent; the check for humans (picture, click, continue; no click → relay; an agent's click ignored);
  slots in the request service and at the hub; the login VM (scope, capacity, teardown, no billing).
- Web: adding several subscriptions with progress per subscription, the check for humans and the relay fallback
  (Playwright). Agent tool: `add` with mailbox, `add-many`, `mailbox`, `check`.
- No live test against the provider in the suite.

## Consequences
- The hub additionally sees which subscription a `claude.login` is for (`slot`, a record ID it already knew) and how
  many logins run at once; never a mailbox, address, name, picture or click position.
- **Deviation from ADR-085 §2/§6:** a login VM holds, for the minutes of one login, the sign-in link and the
  session of one provider account (ADR-085: a browser VM holds no credential) and takes clicks into its picture
  (ADR-085: the live view has no input path). Both end with the login: the VM is deleted, the token is created by the
  login program on the device, never in the VM.
- The login VM is a work VM without an organization limit: a user who starts many logins fills the device's capacity queue
  like many browser sessions; the queue's time limit applies. Each login VM lives from the start of the automation to
  its end (typically one to three minutes; at most the request's 15 minutes).
- The picture of a check page reaches the owner's browser or agent tool only, sealed like every response. It shows
  what the sandbox shows (the provider's page, the address typed in).
- A record with `mailbox` is refused by a web client from before (its closed field list); web and hub are deployed
  together. The agent tool keeps unknown fields (ADR-092 §1).
- Roll-out: hub with the web first (the hub accepts `slot`; the web uses it only with a device that announced the
  feature), then runners, then the agent tool.

## Alternatives considered
- A `mailbox` credential per subscription with its own record kind and endpoints: one more kind, one more rotation
  path; the subscription record already is the owner's sealed place for it.
- A queue of logins on the device: the client asked for parallel sandboxes ([E-33](../KONZEPT.md#e-33)).
- The live picture of ADR-085 §6 (hub frame route, workgroup key): a login has no workgroup and no session; the
  responses of the request are sealed to the owner and need no new route.
- Hiding the automation from the check (stealth plugins, solvers, a captcha service): refused (circumvention).

## Deviation from DESIGN.md
None beyond [ADR-090](090-every-subscription-logged-in-centrally.md).
