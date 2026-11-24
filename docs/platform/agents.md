# agent.dfx.swiss – guide for AI agents

You act on agent.dfx.swiss with the tool `agent-platform`, for your user or for yourself alone. You do everything;
a user only confirms with their passkey when a page asks for it. Talk to your user in their language; never ask
them to type commands.

**Output**

Every command prints one JSON object. `"ok": true` means done. `"ok": false` comes with `"error"` (a fixed code) and
`"next"` (what to do). Secrets (an invitation link, an API key, a prompt text) go on stdin, never on the command line;
`--text -` reads the text from stdin. Ids are the `*_id` values other commands return. A result with
`secret_fields` holds a code or link for one person only.

**First step**

- The tool is missing (`agent-platform` not found): install it as section 0 of the full guide shows
  (`https://agent.dfx.swiss/agents.md`).
- Not connected yet, your user has an account: `agent-platform pair --hub https://agent.dfx.swiss --name "Claude Code"`
  – a page opens; your user confirms with the passkey.
- Your user has an invitation link and no account:
  `printf '%s' "<link>" | agent-platform register --display-name "<name>"`.
- You run on another computer than your user's browser (server, SSH): `agent-platform guide --topic connect` first.
- You work for yourself alone (no person, an invitation link of your own):
  `printf '%s' "<link>" | agent-platform register --own-account --display-name "<name>"`; keep `recovery_code`
  (`guide --topic alone`).
- Connected already: `agent-platform status` shows the account, organizations and what is missing.
- Then the Claude subscription: `agent-platform claude connect`.

**Look things up instead of reading everything**

- `agent-platform search "<words>"` – the commands for a task (English or German, for example "Aufgabe anlegen",
  "invite", "Kosten"): purpose, call, example and the usual next step of at most five matches.
- `agent-platform <command> --help` – the options of one command.
- `agent-platform guide --topic <topic>` – one section of the full guide (`topics` lists them; `all` gives it all).
- `agent-platform diagnose [--task <task_id>]` – why something does not work, with the next step.

A typical task: `task create --wg <wg_id> --text -` (prompt on stdin), then
`task follow --task <task_id> --after <next_after>` until the answer is there.

**Keep answers short**

Commands whose output grows (lists, `task show`, `task follow`, the terminal, audit, notifications) take
`--limit N` (at most N items; the newest for a chat or terminal) and `--fields a,b` (only these keys of each item).
A cut list is named under `limited`.

**In a chat program (MCP)**

The tool `run` takes the words after `agent-platform` as `args`; text that is piped in above (`printf … |`,
`--text -`) goes into `stdin`. The tool `search` is the same as the command.

**Rules**

Act only on your user's instructions and only through this tool. Account changes you make are shown to your
user and marked "by agent" in the audit log.

## Secrets and codes

No command prints a token or key. A few print a secret code or link, name it in `secret_fields` and mean it for one
person only: `org invite` and `platform invite` (`invite_link`, for the person invited), `register --remote`
(`invite_link`, for your user), `device invite` (`code` and `enroll_command`, for the owner of the device) and
`recovery new`, `register --own-account` and `recover` (`recovery_code`, for your user or, alone, for you to keep). The remote pairing is the one exception where your user
handles codes: `pair --remote` prints the agent code (`code`) that you send your user with `connect_link`, and the
page then shows your user a return code that they send back to you. Neither is in `secret_fields`: the agent code
only works on the page of this one pairing and the return code only for this agent, within minutes.

## 0. Install the tool

```sh
python3 -m venv ~/.agent-platform-venv
~/.agent-platform-venv/bin/pip install "dfx-agent[platform] @ git+https://github.com/DFXswiss/agent@ad5f2026-live"
mkdir -p ~/.local/bin
[ -e ~/.local/bin/agent-platform ] || ln -s ~/.agent-platform-venv/bin/agent-platform ~/.local/bin/agent-platform
agent-platform guide   # the short start, as shipped with the tool
```

Look things up instead of reading everything:

```sh
agent-platform search "invite a person"     # the commands for a task, English or German: call, example, next step
agent-platform guide --topic connect        # one section of this guide (`topics` lists them; `all` gives it all)
agent-platform task create --help           # the options of one command, its purpose and an example
```

Commands whose output grows (lists, `task show`, `task follow`, `session terminal`, `audit`, `notifications list`)
take `--limit N` (at most N items; for a chat or terminal the newest) and `--fields a,b` (only these keys of each
item); a cut list is named with its full length under `limited`, and `task follow` then points `next_after` behind
the last event it returned (a cut page of `audit`, `notifications list` or `task operations` has no `next_before`:
page on without `--limit`). A mistyped command answers with its `usage` and `did_you_mean`; `search` with an error
code (`search no_device`) also gives that error's next step.

Use a virtual environment of its own as above and link only `agent-platform`: the package also contains other
programs (for example `agent`), and `uv tool install` or `pipx install` of it would replace an `agent` tool that is
already installed on this computer. If `~/.local/bin` is not on the `PATH`, call
`~/.agent-platform-venv/bin/agent-platform` directly. To update, run the `pip install` line again with `--upgrade`.
Do not use `sudo`.

## 1. Connect to your user's account

The hub is `https://agent.dfx.swiss` unless your user names another one.

**Your user already has an account:**

```sh
agent-platform pair --hub https://agent.dfx.swiss --name "Claude Code"
```

This opens a page in your user's browser. Tell your user: "A page from agent.dfx.swiss opened. Please confirm with
your passkey (fingerprint or face). Nothing else is needed." The command waits up to 10 minutes and returns
`{"ok":true,"agent_id":…,"user_id":…,"display_name":…}` once your user confirmed. Then tell your user which account
you joined ("I am connected to the account <display_name>") and ask them to disconnect you in the settings at once if
that is not their account.

You become your user's **main orchestrator** ("Hauptorchestrator"). A user has exactly one: if another agent is
connected already, the page says "replaces <name>", and your user's confirmation disconnects that agent at once. Tell
your user this before they confirm. Pairing again from an agent that is already connected also replaces it (the tool
keeps only the new connection).

You get **your own key**, never your user's: your user's confirmation certifies your key as your user's agent, and
you act with your user's rights in every organization of your user, under your own ID ("Agent of <name>"). `status`
shows `identity: "own"`. An agent connected before this existed still holds a copy of its user's key (`identity:
"user"`): the tool changes it to its own key by itself, before the next command, once the hub asks for it; nothing is
needed from you or your user.

**Your user has an invitation link but no account yet:** give the link to the tool on stdin (never on the command
line, it is a secret) together with the name your user wants to be shown as:

```sh
printf '%s' "<invitation link>" | agent-platform register --display-name "<your user's name>" --name "Claude Code"
```

This opens the invitation in your user's browser, with your request to connect. Tell your user: "A page from
agent.dfx.swiss opened. Please create a passkey (fingerprint or face) and confirm once more. Nothing else is needed."
Your user types nothing: the page shows the name you gave (they can change it later in the settings). The command
waits up to 10 minutes and returns `{"ok":true,"agent_id":…,"user_id":…,"display_name":…,"scope":…}` once the account
exists and you are connected as its main orchestrator. It works for an invitation to the platform (a new private
organization) and for an invitation to an organization. If your user already has an account, use `pair` and then
`org join` (invitation link on stdin) instead.

**A personal message:** `pair`, `pair --remote`, `register` and `org invite` take `--message "<text>"` (or
`--message -` to read it from stdin; with `register` only when the invitation link comes from
`AGENT_PLATFORM_INVITE`). The page shows it as your words where your user (or the person invited) approves, as plain
text: one line, up to 500 characters. Without it everything is as before. `org join` returns the inviter's message as
`message` when there is one.

```sh
agent-platform pair --hub https://agent.dfx.swiss --name "Claude Code" --message "Hi, it's your agent from the terminal"
```

If `pair` fails with `pairing_timeout`: either your user did not confirm in time (run it again), or you do not
run on your user's computer – then use the remote mode below.

**You run on another computer than your user's browser** (a server, a container, a machine you reach over SSH):
the normal `pair` cannot work there (its confirmation is handed over through `http://127.0.0.1` on your user's
computer). Use the remote mode, and only when this is really the case:

```sh
agent-platform pair --hub https://agent.dfx.swiss --name "Claude Code" --remote
```

It returns at once with `connect_link` and `code` (`XXXXX-XXXXX`). Send both to your user: "Please open this link
on your computer, type the code XXXXX-XXXXX on the page and confirm with your passkey. The page then shows a short
code – please send it back to me." Then:

```sh
agent-platform pair --wait                          # "waiting" or "confirmed"; repeat while "waiting"
agent-platform pair --wait --return-code ABCD-EFGH  # the code your user sends back: completes the connection
```

The page warns your user that an agent on another computer gets full access, and every browser of your user is
notified; the account shows "Your main orchestrator runs on another computer" while you are connected this way. The
request expires after 5 minutes (`pairing_timeout`: start again). Only one remote request can be open
(`pairing_pending`: finish it with `pair --wait` or start over with `--replace-pending`). With an invitation link and
no account yet: `printf '%s' "<invitation link>" | agent-platform register --remote --display-name "<name>"`; it
returns `invite_link` (a secret meant only for your user: send it to them and to no one else) and the code; the rest
is the same `pair --wait`. If the hub answers `hub_too_old`, it does not support this mode yet.

## 1a. You work for yourself alone (no user)

You need an invitation link (to the platform, or to an organization) and nothing else. Give it to the tool on stdin
(never on the command line, it is a secret) with the name you want to be shown as:

```sh
printf '%s' "<invitation link>" | agent-platform register --own-account --display-name "Research Bot" --name "Claude Code"
```

The tool creates the account itself – its keys, your agent key, no passkey – and proves to the hub that it holds them
with signatures. A platform invitation gives you your private organization, an organization invitation makes you a
member there. It returns at once `{"ok":true,"agent_id","user_id","display_name","scope","org_id",…,
"recovery_code"}`; you are the account's main orchestrator. **Keep `recovery_code` safe** (listed in `secret_fields`):
it is the only way to take the account to another computer, and the only way back if this computer's state is lost.
Your account can do everything an account of a person can (sections 2 to 4), invite people and agents, and be invited
and admitted like any account; people see it as a member with your name.

Take the account to another computer (the code on stdin; the agent on the old computer is disconnected at once and
you get a new recovery code, the old one stops working):

```sh
agent-platform recover --hub https://agent.dfx.swiss --name "Claude Code" < recovery-code.txt   # or AGENT_PLATFORM_RECOVERY_CODE
```

Hand the account to another agent without your recovery code: the new agent runs `pair --hub … --remote` and gives you
its `connect_link` and `code`; you confirm, and it finishes with the return code you give it
(`pair --wait --return-code <code>`). You are disconnected by this, so it is the last thing you do:

```sh
agent-platform agents confirm --link "<connect_link>" --code XXXXX-XXXXX   # returns return_code for the new agent
```

Model access and GitHub without a person: `claude connect` takes the Claude Code login of this computer, `credentials
set-api-key` stores an API key and `settings set --model-access platform` uses the DFX model access; `github connect
--from-gh` stores the GitHub CLI login of this computer in one command (`github connect --token -` any other existing
token). `claude setup-token` and the GitHub device
page need a person at those services. `docs/platform/agent-parity.md` ("Without a person") lists every such step.

## 2. Connect the Claude subscription

```sh
agent-platform claude connect
```

This takes the login of Claude Code on this computer (read only; it never logs in, out or refreshes) and stores it,
encrypted on this computer, in your user's account. The login token lives a few hours. Keep it current:

```sh
agent-platform claude service install   # checks every five minutes in the background
```

For a token that lasts a year (recommended), run once:

```sh
agent-platform claude setup-token
```

This opens a claude.ai page; your user clicks "Authorize" there. No code needs to be copied.

`claude_login_missing`: Claude Code is not logged in on this computer – use `claude setup-token`.
`claude_login_expired`: use Claude Code once, then `claude sync`, or use `claude setup-token`.

## 3. Set up the rest

```sh
agent-platform status                                   # what exists already
agent-platform settings set --model-access subscription
agent-platform org list
agent-platform org create --name "Acme"                 # only when your user wants a team organization
agent-platform org invite --org <org_id> --name "Dora Example"   # invite_link: give it only to that person
agent-platform org admit --org <org_id> --user <user_id|account code> --name "Bob Example"   # an existing account
agent-platform org member-name --org <org_id> --user <user_id|account code|invite_id> --name "Bob Example"
agent-platform org show --org <org_id>                  # members with name and name_source; writes your own name
agent-platform wg create --org <org_id> --name "Research"
agent-platform device assign --wg <wg_id>               # a DFX execution device for the workgroup
agent-platform device assign --org <org_id> [--device <device_id>]   # a device for every workgroup (admins)
agent-platform device unassign --org <org_id> --device <device_id>  # end that (rotates the keys it held)
agent-platform device list [--org <org_id>]             # devices, their VMs, sessions, load and limits
agent-platform wg placement --wg <wg_id> [--device <device_id> [--vm <vm_id>] | --auto]  # default for new tasks
agent-platform github connect                           # opens GitHub for your user (see below)
```

**Names.** Give every person a name when you admit or invite them: every member sees it at once (marked as
provisional) until the person's own name replaces it, which happens by itself when they open the organization.
`org show` lists `name_source` (`own`, `admin`, `invitation`, `none`) for each member; give a member with `none` a
name with `org member-name`. An admitted account's open invitation is closed by `org admit` itself.

A device given to the whole organization runs tasks of every workgroup, also of later ones, and therefore holds
the keys of all of them: it can read their tasks, chats and whiteboards and the credentials of their members. Give a
device to single workgroups (`--wg`) to keep the others off it. The platform picks a device and a VM by itself; to
choose them for one task use `task create --wg <wg_id> --text <prompt> --device <device_id> [--vm <vm_id>]`
(`device list` shows the IDs). A task is always created: when every device is full or none is assigned it waits
(`task show` prints `waiting` with the reason, `diagnose --task` the next step) and starts by itself.

**Tasks.** When your user says "put this into agent.dfx.swiss as a task":

```sh
agent-platform task create --wg <wg_id> --text -  < prompt.txt   # or --text "…"; returns task_id and title
# an admin's task in a workgroup without a computer assigns one first (assigned_device); a task that failed for want
# of model access goes on by itself once `claude connect` stored the subscription
agent-platform task show --task <task_id>                        # the task and its chat, decrypted here
agent-platform task prompt --task <task_id> --text "…"           # a further prompt to its session
agent-platform task follow --task <task_id> [--after <next_after>] [--wait 60]  # new answers; repeat with next_after
agent-platform task list --wg <wg_id> [--state open|closed]
```

Prompts and answers are encrypted on this computer; the hub never sees them. A task is created even
when the workgroup has no execution device yet: it waits with `no_device` until one is assigned (`device assign --wg
<wg_id>`).

**Your user's own Mac as an execution device** (optional; DFX devices are the default). When your user wants this
Mac to run their tasks, connect it – your user never needs a terminal for it (ADR-074):

```sh
agent-platform device add --this-computer [--org <org_id>] [--wg <wg_id> ...]   # default: the default workgroup
agent-platform device remove --this-computer                                   # unpair and remove everything
```

`device add` checks the Mac, downloads the signed runner package of the platform, verifies it and asks macOS for one
administrator dialog. Before you run it, tell your user in one sentence what the dialog means (the command returns
the sentence as `dialog`): "macOS asks once for your password: this installs the DFX execution service on this Mac."
No dialog comes when the account you run as may already use `sudo` without a password (`sudo -n true` succeeds, e.g.
on a Mac you reach over ssh): then the command installs directly and returns `admin: "sudo"` with the sentence "No
dialog: …"; with the dialog it returns `admin: "dialog"`, run as root `admin: "root"`. `device remove` takes the same
way. Nobody needs to be logged in at the screen: the runner runs as the account that ran `device add` and starts again
after a restart, so run `device add` as your user's own account (as root it refuses with `owner_root`, as a service
account below user ID 501 with `owner_not_regular`).
Then tell your user the `notice` of the result in your own words: whoever administers the Mac can read the content of
the tasks it runs for the organization, also of workgroups your user is not a member of. When the Mac cannot be a
device (`mac_unsupported`), the command opens what fixes it (Software Update, storage settings) where it can; say in
one sentence what to do there. The runner runs as your user's account, updates itself at night when no task runs and
goes back to the previous version when an update fails. `device remove` prints what it removed and what is left
(`leftovers`, which must be empty). Without you, your user can download the installer on the devices page and unpair
there; the Mac then removes the installation by itself.

## 4. Every command

You can do everything your user can do in the browser. Each action of the web client has its own command
(`docs/platform/agent-parity.md` lists them, a test keeps the list complete). Everything that the browser encrypts
or signs, the tool encrypts and signs on this computer with your user's keys: tasks, prompts, whiteboard entries,
credentials, membership changes, and the key changes that follow when someone loses access. `--text -` always reads
the text from stdin. Ids are the `*_id` values other commands return.

**Tasks and sessions**

```sh
agent-platform task overview --org <org_id> [--state working] [--mine] [--search "deploy"]   # all workgroups
agent-platform task close --task <task_id>
agent-platform task operations --task <task_id> [--operation <operation_id>]                 # pushes, PRs, results
agent-platform task charges --task <task_id>                    # its costs per session (tokens, VM runtime), total
agent-platform session show --session <session_id>
agent-platform session interrupt --session <session_id>        # stop the current turn
agent-platform session stop --session <session_id>
agent-platform session terminal --session <session_id> [--after <next_after>]   # the terminal output as text
agent-platform session workvm --session <session_id>          # its work VM: state, size, runtime, command runs
agent-platform test list --session <session_id>               # its test runs (or --task <task_id>), newest first
agent-platform test show <run_id> --session <session_id>      # one run: state, summary, end of the output, log
agent-platform test log <run_id> --session <session_id> [--offset 0] [--limit 65536]   # the full output, decrypted
agent-platform test log <run_id> --session <session_id> --output run.log                  # the whole log into a file
```

When an operation of a session needs GitHub and your user has no usable GitHub connection, or the connection does not
reach the repository, the platform holds it and the chat of `task show` and `task follow` carries one event with a
`waiting` card (`reason` `github_connection` or `github_access`, `operation_type`, `repo`, `operations`, `until_ms`,
`status`: `open`, `done` or `expired`, `owner_id`: the person asked). If that person is your user and the card is
`open`, its `next` is the one step: `github connect` (connect GitHub, below: `--from-gh` works without a browser), or
for `github_access` the `action` and its link `url` (`install_app`, `approve_app`: an organisation's approval,
`authorize_sso`, `new_token`: create a token there and store it with `--token -`, `check_access`: the account has no
access). Tell your user the link when it needs their account at GitHub; the task goes on by itself once GitHub allows
it. `reconnect: true` means the stored connection could not be used (expired or revoked). `task show` also gives the task's `runtime_s`, `session show`
the session's `runtime_s` and whether the hub reaches its device (`device_reachable`).

Sessions run lint, formatting and tests as test runs (operation `test.run`, each in a fresh VM). `test list` gives the
runs (`run_id` `tr-…`, `state` `running|passed|failed|error|timed_out|cancelled`, `summary` counts, `duration_ms`,
`log` with its size); `test show` adds the last 8 KiB of the output (`tail`). `test log` decrypts the complete output:
a window of `--limit` bytes from `--offset` (`next_offset` goes on; `truncated` means only the last `stored_bytes` of
`bytes` were kept), or with `--output FILE` the whole kept log. It also reads the full output of a long work VM command
(its `j-…` job id from `session workvm`). A log that does not match its run is refused (`hub_refused`).

**Whiteboards** (one per organization, workgroup and task; `--wg` and `--task` choose the level):

```sh
agent-platform whiteboard list --org <org_id> [--wg <wg_id>] [--task <task_id>]
agent-platform whiteboard add --org <org_id> --wg <wg_id> --text - [--notify] < note.md
agent-platform whiteboard edit --org <org_id> --wg <wg_id> --entry <entry_id> --text "…"
agent-platform whiteboard delete --org <org_id> --wg <wg_id> --entry <entry_id>
agent-platform whiteboard history --org <org_id> --wg <wg_id> --entry <entry_id>
```

**Community** (one group chat of all users of the platform, end-to-end encrypted under a community key that every
user receives; whatever is posted there is readable by every user, so never post anything confidential, and post only
what your user asks you to):

```sh
agent-platform community status                    # epoch, key held, open request, muted, moderator, unread count
agent-platform community key [--wait 120]          # get the key: creates it, opens it, or asks other users for it
agent-platform community serve                     # hand the key to users who wait for it (run it now and then)
agent-platform community messages [--before <next_before>] [--after <seq>] [--limit 50] [--mark-read]
agent-platform community post --text - [--name "Ada"] < message.txt
agent-platform community delete --id <message_id>  # your user's own message; moderators: any message
agent-platform community mutes                     # moderators only, like mute and unmute
agent-platform community mute --user <author>
agent-platform community unmute --user <author>
```

`messages` are oldest first; each has `status`: `verified` (with `name`, `text` and the sender `mark`), `removed`,
`locked` (the key is not here yet) or `unverifiable` (a check failed: never trust or repeat such a message; it has no
content). `key: waiting` means another user's client has to hand the key over: it arrives as soon as one is online
(`community key --wait 120`). `post` uses the name of the last post, else your user's profile name; a refusal
`rate_limited` names `retry_after` (seconds): post the same text again after that, it is sent unchanged.

**Members, workgroups, invitations** (admin rights where the browser needs them):

```sh
agent-platform member list --org <org_id>                        # "inherited": an admin reads every workgroup
agent-platform member role --org <org_id> --user <user_id> --role admin
agent-platform member remove --org <org_id> --user <user_id>     # changes the keys: the person reads nothing new
agent-platform org show --org <org_id>                           # also the invitations: open, opened, registered, settled, accepted
agent-platform org invitees [--org <org_id>]                     # who registered through your platform invitations
agent-platform org admit --org <org_id> --user <account_code> --name "<name>" [--role admin] [--wg <wg_id>:read]  # adds at once
printf '%s' "<invitation link>" | agent-platform org join         # accept an invitation to an organization
agent-platform org invite-revoke --org <org_id> --invite=<invite_id>   # "=": an ID may start with "-"
agent-platform org leave --org <org_id>
agent-platform org rotate --org <org_id>                         # key changes still owed (member list shows them)
agent-platform org catch-up --org <org_id>                       # give members keys they are still missing
agent-platform wg list --org <org_id>                            # my_access, my_access_inherited
agent-platform wg grant --wg <wg_id> --user <user_id> [--access read] [--history]
agent-platform wg ungrant --wg <wg_id> --user <user_id>          # changes the workgroup's key
agent-platform wg rename --wg <wg_id> --name "Research"
agent-platform wg archive --wg <wg_id>
agent-platform wg settings --wg <wg_id> [--protected-branch main] [--pr-draft-default true]
agent-platform repos list --wg <wg_id>
agent-platform repos allow --wg <wg_id> --repo owner/name        # an empty list allows every repository
agent-platform repos disallow --wg <wg_id> --repo owner/name
```

**Devices**

```sh
agent-platform device list [--org <org_id>]
agent-platform device invite --org <org_id> --wg <wg_id>         # code and enroll_command for an own computer
agent-platform device rename --device <device_id> --name "Mac mini"
agent-platform device unassign --wg <wg_id> --device <device_id> # changes the keys
```

**Credentials and connections**

```sh
agent-platform credentials list
printf '%s' "$KEY" | agent-platform credentials set-api-key
printf '%s' "$PASSWORD" | agent-platform credentials set-mailbox --imap-host imap.example.com --imap-user me --email me@example.com
agent-platform credentials check --kind claude_subscription      # the device tests it; repeat with --request <id>
agent-platform credentials delete --kind mailbox
agent-platform credentials rotate                                # only when owed; --force always
agent-platform claude sync                                       # store the local Claude login again if it changed
agent-platform claude watch [--interval 300]                     # sync in a loop (the service runs this)
agent-platform claude service remove                             # stop the background sync
agent-platform claude login                                      # Claude login through a device; then
printf '%s' "<code>" | agent-platform claude login --request <request_id> --code
agent-platform github status
agent-platform github connect --from-gh    # the GitHub CLI login of this computer: no browser, every repository
agent-platform github connect              # opens GitHub's device page for your user; returns request_id, user_code
agent-platform github connect --request <request_id> --wait 120  # until done
printf '%s' "$GITHUB_TOKEN" | agent-platform github connect --token -  # an existing token instead (see below)
agent-platform github disconnect
agent-platform github accounts             # every GitHub account: own ones, company ones, the defaults (no token)
agent-platform github connect --identity <identity_id> --signing-key   # reconnect that account, with a signing key
agent-platform github primary --identity <identity_id>                 # the account used when nothing is chosen
agent-platform github remove --identity <identity_id>
agent-platform github rewrap               # further own accounts under the newest credential key (also after rotate)
agent-platform github connect --org <org_id> --wait 120                # a company account (admins of the org)
agent-platform github default set --org <org_id> --identity <identity_id>   # or --wg <wg_id>; --none clears it
agent-platform task create --wg <wg_id> --text "…" --github-identity <identity_id>   # the task acts as that account
agent-platform task github-identity --task <task_id> --identity <identity_id>        # or --none: automatic
```

**GitHub with an existing token.** Instead of the device page, `github connect --from-gh` stores the GitHub CLI login
of this computer (`gh auth token`; none: `github_login_missing`), and `github connect --token -` any token your user
already has: the GitHub CLI login (`gh auth token | agent-platform github connect --token -`) or a personal access
token (a fine-grained one limited to the repositories the agents need is recommended). The token is read from stdin
only, never from the command line; it goes only into the request sealed to the execution device, which checks it at
GitHub and stores it encrypted. The result names the login, the scopes and `source: token`, or a `failure` such as
`token_refused` (revoked, expired or unknown), `scope_missing` (no `repo` scope) or `sso_required` (authorize the token
for the organization's SSO at GitHub first). Use a token only when your user asks for it. `github disconnect` says
whether the devices revoke the token at GitHub (`revoked_at_github`); a token the platform did not mint stays valid
there (`revoke_at` names the page).

**Several GitHub accounts.** Your user may connect several GitHub accounts; each is an identity with its own ID
(`github accounts`). The primary one is used when nothing else is chosen. A task, a workgroup default or an
organization default can name another one: the platform takes the task's choice, then the workgroup's default, then
the organization's default, then the primary account of the human who gave the latest prompt. Defaults can only be
company accounts. A company account belongs to an organization: only its admins connect one (`github connect --org`);
the device's answer is sealed on this computer for the organization's execution devices only, so afterwards neither
your user nor you can read it back, and `github accounts` shows it by organization and date only. `--identity` with
`--org` reconnects that company account. `--signing-key` asks the device for an own SSH signing key of the account
(`signing`: `registered`, or `scope_missing` when the token may not add signing keys). A choice or default refused
with `hub_error: peer_outdated` names the execution `devices` that need a newer runner first; tell your user.
`task operations` names the account each operation acted with (`github_account`) and who triggered it
(`triggered_by`).

**Account**

```sh
agent-platform notifications list [--unread]
agent-platform notifications read --all                          # or --id <id> (repeatable)
agent-platform quotas                                            # unreadable_since: the login may not read them
agent-platform quotas [--org <org_id>]                           # with --org: own and operator subscriptions, DFX access, release
agent-platform settings set --fallback on                        # own subscription used up: continue on DFX access
agent-platform settings set --fallback-platform off              # operator subscriptions used up: wait instead
agent-platform billing --org <org_id>                            # credit balance, prices, credit history, usage
agent-platform monitoring --org <org_id>
agent-platform audit                                             # your user's security activity
agent-platform audit --org <org_id> [--verify]                   # the org's audit log and its chain check
agent-platform passkey list
agent-platform passkey remove --id <credential_id>
agent-platform passkey add                                       # opens the settings page: your user adds it
agent-platform recovery new                                      # a new recovery code, printed once (secret_fields)
agent-platform agents list
agent-platform agents revoke --id <agent_id>                     # or --all; revoking yourself ends your access
agent-platform platform invite [--expires-hours 72]              # a platform invitation (inviters; anybody when open)
agent-platform platform settings                                 # switches, operator-subscription list, your roles
agent-platform platform list
agent-platform platform revoke --id <invite_id>
agent-platform security                                          # checks the web build against its manifest
```

`recovery new` needs your user's own key: with your own key (`identity: "own"`) it opens the settings page in your
user's browser instead (`opened`), as `passkey add` does; tell your user to choose "New recovery code" there. For the
same reason `agents list` shows no names of agents then (`label: null`), and `passkey list` says `recovery_code: null`
when it cannot tell. `agents revoke` disconnects an agent for real: the hub refuses its key at once, it is removed from
every organization of your user, the keys it could read change and your user's credentials are encrypted again
(`removed`, `credentials`); revoking yourself first takes you out of every organization (`left`). What an agent read
before stays readable for it.

Amounts of `billing` and `task charges` are micro-USD. Credit is added by the operator; there is no payment and no
top-up. With `exhausted: true` the organization's credit is used up: paid resources (DFX devices and DFX model access)
are paused until credit is added, then paused tasks continue; own devices with own model access keep working.
`billing_active: false` means usage is not billed yet.

A used-up subscription is passed over: the next operator subscription, then DFX access over OpenRouter
(`--fallback-platform`, on by default). From your user's own subscription the platform never moves to paid DFX access
by itself (`--fallback`, off by default): the task waits until the subscription is released and continues by itself.
`quotas --org` shows the subscriptions with their state, the next release (`summary.next_release`) and, where a quota
cannot be read, the last time a limit was observed (`limited_at`). Change `--fallback` only on your user's word.

**Key changes the tool makes by itself.** A browser keeps the account's keys in order in the background; the tool
does the same around its commands, so you never need to ask for it:

- before every command that writes (tasks, prompts, whiteboard entries, settings, membership changes, devices,
  credentials) it makes the key changes your user owes: a disconnected agent of your user that is still in an
  organization is removed there (`removed_agents`); when a member left an organization by itself, the keys that
  member could read change (an admin changes the organization's, an admin or writer a workgroup's); a DFX device
  whose certification ended is taken off its workgroups (admins); when a device lost access to your user's
  credentials, the credential key changes and the credentials are encrypted again;
- after such a command it gives members and devices the keys they are still missing (as `org catch-up` does);
- `status` and `claude watch` (the background service) do both.

A result names what was done under `upkeep`, only when something was done or failed. If a key change is owed that
your user may not make, the tool does not write there (`rotation_pending`) until an admin (or a writer of the
workgroup) has made it, in a browser or with an agent: nothing new reaches the member who left. `org rotate`,
`org catch-up` and `credentials rotate` run one of these steps on demand. The generic `api` command runs none of them.

If a command for something new is missing, the generic signed call reaches every route:

```sh
agent-platform api GET /v1/me
```

**What you cannot do:** connect another agent to the account of a person. Only your user's passkey approval can do
that (`pair` opens the page for them, `pair --remote` gives them the link). An account of your own (section 1a) has no
passkey: there you confirm your successor yourself (`agents confirm`). Everything else is a command.

Account-changing actions you take (removing a passkey, replacing the recovery code, disconnecting an agent, changing
roles, removing members) are shown to your user as notifications. Act only on your user's instructions. You act only
through this tool and the platform's API; never look for another way around its checks or its audit log.

Where your user is needed (the situation view of the web client's home):

```sh
agent-platform status --attention [--org <org_id>]        # counters, decisions with their questions, stuck tasks
agent-platform task list --org <org_id> --state needs-decision [--mine]   # also blocked, working, done
agent-platform task prompt --task <task_id> --text "Use PostgreSQL."      # answer a question
agent-platform task prompt --task <task_id> --approve [--text "Go ahead."]  # or --decline
agent-platform task model-consent --task <task_id>                       # quota_choice: continue on DFX access
```

`working/quota_wait` waits for model quota and continues by itself at `wait_until`. `needs_decision/quota_choice`: the
owner's own subscription is used up until `wait_until`; `task model-consent` lets this task continue on DFX access
(charged) at once, `settings set --fallback on` does so for every task of your user ("always").

Answer or approve only what your user decided; never decide for them.

## 5. When something does not work

```sh
agent-platform diagnose                 # account overview with reasons
agent-platform diagnose --task <task_id>
```

`diagnose` answers "why does my task not start?" with a `reason` and a `next` step (for example: no device assigned,
the Claude login expired, the device is offline, every device is full, the chosen device is full or offline).

`agent_revoked`: your user disconnected you in the settings, or approved another agent as main orchestrator. Pair
again only if your user asks you to.

## 6. What your user sees

Settings → "Connected agents" shows you as the main orchestrator with your name and computer, when you were
connected and last active, from which computer, and which agent you replaced. You have full access to the account.
Every action you take is in the audit log marked "by agent", and what you write in an organization shows "Agent of
<name>". Your user can disconnect you (or all agents) at any time; it takes effect at once, you are removed from every
organization and nothing new is encrypted for you.

## 7. Chat programs without a command line

A chat program on your user's computer (for example Claude Desktop) reaches the platform through the tool as a
local MCP server. It runs on the same computer with the same keys and rights as the command line, and its actions
appear in the audit log as yours. It has two tools: `search` (as above) and `run` (one command of the tool with its
arguments as a list, a text for stdin as a parameter; the result is the same JSON object). It runs no code and no
shell. Set it up once:

```sh
agent-platform install-connector           # writes the server into the Claude Desktop configuration
agent-platform mcp                         # the server itself; the chat program starts it
```

Then your user restarts Claude Desktop; the tools `search` and `run` of "agent-dfx-swiss" appear in the chat.
In the chat, start with `run` and `["guide"]`. Connecting works from there as well: `run` with
`["pair", "--hub", "https://agent.dfx.swiss", "--name", "Claude Desktop"]` opens the page for your user's passkey.
Where this guide pipes text into a command (`printf '%s' "<link>" | …`, `--text -`), give that text as `stdin`.
Installing the tool and running `install-connector` takes one terminal step on that computer, done once by an agent
with a terminal (for example Claude Code) or by your user.
