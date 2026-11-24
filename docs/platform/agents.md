# agent.dfx.swiss – guide for AI agents

You are an AI agent (for example Claude Code) on your user's computer. Your user said something like "set up
agent.dfx.swiss for me". This guide tells you exactly what to do. Your user does **one** thing: confirm with their
passkey in the browser window you open. You do everything else. Talk to your user in their language and never ask
them to type commands or open settings; the only codes your user ever passes on are the two of the remote pairing
(section 1), when you run on another computer.

**Two cases.** _You have a user_ (a person whose account you set up and act for): follow section 1. _You work for
yourself alone_ (no person, for example an autonomous agent with an invitation link of its own): follow section 1a –
no step there needs a person, a browser or a passkey. From section 2 on everything is the same for both.

The tool for all of this is `agent-platform` (package of the `DFXswiss/agent` repository). Every command prints one
JSON object. `"ok": true` means done. `"ok": false` comes with `"error"` (a fixed code) and `"next"` (what to do).
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
agent-platform guide   # this guide, as shipped with the tool
```

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
--token -` stores an existing GitHub token (for example `gh auth token`). `claude setup-token` and the GitHub device
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
agent-platform github connect                           # opens GitHub for your user (see below)
```

**Names.** Give every person a name when you admit or invite them: every member sees it at once (marked as
provisional) until the person's own name replaces it, which happens by itself when they open the organization.
`org show` lists `name_source` (`own`, `admin`, `invitation`, `none`) for each member; give a member with `none` a
name with `org member-name`. An admitted account's open invitation is closed by `org admit` itself.

**Tasks.** When your user says "put this into agent.dfx.swiss as a task":

```sh
agent-platform task create --wg <wg_id> --text -  < prompt.txt   # or --text "…"; returns task_id and title
agent-platform task show --task <task_id>                        # the task and its chat, decrypted here
agent-platform task prompt --task <task_id> --text "…"           # a further prompt to its session
agent-platform task follow --task <task_id> [--after <next_after>] [--wait 60]  # new answers; repeat with next_after
agent-platform task list --wg <wg_id> [--state open|closed]
```

Prompts and answers are encrypted on this computer; the hub never sees them. `task create` fails with `no_device`
when the workgroup has no execution device: run `device assign --wg <wg_id>` first.

**Your user's own Mac as an execution device** (optional; DFX devices are the default). When your user wants this
Mac to run their tasks, connect it – your user never needs a terminal for it (ADR-074):

```sh
agent-platform device add --this-computer [--org <org_id>] [--wg <wg_id> ...]   # default: the default workgroup
agent-platform device remove --this-computer                                   # unpair and remove everything
```

`device add` checks the Mac, downloads the signed runner package of the platform, verifies it and asks macOS for one
administrator dialog. Before you run it, tell your user in one sentence what the dialog means (the command returns
the sentence as `dialog`): "macOS asks once for your password: this installs the DFX execution service on this Mac."
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
```

When an operation of a session needs GitHub and your user has no usable GitHub connection, the platform holds it and
the chat of `task show` and `task follow` carries one event with a `waiting` card (`operation_type`, `repo`,
`operations`, `until_ms`, `status`: `open`, `done` or `expired`). If your user owns the task and the card is `open`,
its `next` is `github connect`: connect GitHub (below) and the task goes on by itself; `reconnect: true` means the
stored connection could not be used (expired or revoked). `task show` also gives the task's `runtime_s`, `session show`
the session's `runtime_s` and whether the hub reaches its device (`device_reachable`).

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
agent-platform github connect              # opens GitHub's device page for your user; returns request_id, user_code
agent-platform github connect --request <request_id> --wait 120  # until done
printf '%s' "$GITHUB_TOKEN" | agent-platform github connect --token -  # an existing token instead (see below)
agent-platform github disconnect
```

**GitHub with an existing token.** Instead of the device page, `github connect --token -` stores a token your user
already has: the GitHub CLI login (`gh auth token | agent-platform github connect --token -`) or a personal access
token (a fine-grained one limited to the repositories the agents need is recommended). The token is read from stdin
only, never from the command line; it goes only into the request sealed to the execution device, which checks it at
GitHub and stores it encrypted. The result names the login, the scopes and `source: token`, or a `failure` such as
`token_refused` (revoked, expired or unknown), `scope_missing` (no `repo` scope) or `sso_required` (authorize the token
for the organization's SSO at GitHub first). Use a token only when your user asks for it. `github disconnect` says
whether the devices revoke the token at GitHub (`revoked_at_github`); a token the platform did not mint stays valid
there (`revoke_at` names the page).

**Account**

```sh
agent-platform notifications list [--unread]
agent-platform notifications read --all                          # or --id <id> (repeatable)
agent-platform quotas                                            # unreadable_since: the login may not read them
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
agent-platform platform invite [--expires-hours 72]              # a platform invitation (inviters only)
agent-platform platform list
agent-platform platform revoke --id <invite_id>
agent-platform security                                          # checks the web build against its manifest
```

Amounts of `billing` and `task charges` are micro-USD. Credit is added by the operator; there is no payment and no
top-up. With `exhausted: true` the organization's credit is used up: paid resources (DFX devices and DFX model access)
are paused until credit is added, then paused tasks continue; own devices with own model access keep working.
`billing_active: false` means usage is not billed yet.

**Key changes the tool makes by itself.** A browser keeps the account's keys in order in the background; the tool
does the same around its commands, so you never need to ask for it:

- before every command that writes (tasks, prompts, whiteboard entries, settings, membership changes, devices,
  credentials) it makes the key changes your user owes: when a member left an organization by itself, the keys that
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

## 5. When something does not work

```sh
agent-platform diagnose                 # account overview with reasons
agent-platform diagnose --task <task_id>
```

`diagnose` answers "why does my task not start?" with a `reason` and a `next` step (for example: no device assigned,
the Claude login expired, the device is offline).

`agent_revoked`: your user disconnected you in the settings, or approved another agent as main orchestrator. Pair
again only if your user asks you to.

## 6. What your user sees

Settings → "Connected agents" shows you as the main orchestrator with your name and computer, when you were
connected and last active, from which computer, and which agent you replaced. You have full access to the account. Every action you take is in the audit log marked "by agent". Your
user can disconnect you (or all agents) at any time; it takes effect at once.
