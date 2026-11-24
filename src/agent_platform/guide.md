# agent.dfx.swiss – guide for AI agents

You are an AI agent (for example Claude Code) on your user's computer. Your user said something like "set up
agent.dfx.swiss for me". This guide tells you exactly what to do. Your user does **one** thing: confirm with their
passkey in the browser window you open. You do everything else. Talk to your user in their language and never ask
them to type commands, copy codes or open settings.

The tool for all of this is `agent-platform` (package of the `DFXswiss/agent` repository). Every command prints one
JSON object. `"ok": true` means done. `"ok": false` comes with `"error"` (a fixed code) and `"next"` (what to do).
No command ever prints a token, key or code, except `org invite`, whose `invite_link` (listed in `secret_fields`) is
meant only for the person being invited.

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
agent-platform org invite --org <org_id>                # returns invite_link: give it only to that person
agent-platform wg create --org <org_id> --name "Research"
agent-platform device assign --wg <wg_id>               # a DFX execution device for the workgroup
agent-platform github connect                           # opens GitHub for your user (see below)
```

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
agent-platform session show --session <session_id>
agent-platform session interrupt --session <session_id>        # stop the current turn
agent-platform session stop --session <session_id>
agent-platform session terminal --session <session_id> [--after <next_after>]   # the terminal output as text
agent-platform session workvm --session <session_id>          # its work VM: state, size, runtime, command runs
```

**Whiteboards** (one per organization, workgroup and task; `--wg` and `--task` choose the level):

```sh
agent-platform whiteboard list --org <org_id> [--wg <wg_id>] [--task <task_id>]
agent-platform whiteboard add --org <org_id> --wg <wg_id> --text - [--notify] < note.md
agent-platform whiteboard edit --org <org_id> --wg <wg_id> --entry <entry_id> --text "…"
agent-platform whiteboard delete --org <org_id> --wg <wg_id> --entry <entry_id>
agent-platform whiteboard history --org <org_id> --wg <wg_id> --entry <entry_id>
```

**Members, workgroups, invitations** (admin rights where the browser needs them):

```sh
agent-platform member list --org <org_id>                        # "inherited": an admin reads every workgroup
agent-platform member role --org <org_id> --user <user_id> --role admin
agent-platform member remove --org <org_id> --user <user_id>     # changes the keys: the person reads nothing new
agent-platform org show --org <org_id>                           # also the open invitations
agent-platform org invitees [--org <org_id>]                     # who registered through your platform invitations
agent-platform org admit --org <org_id> --user <account_code> [--role admin] [--wg <wg_id>:read]  # adds at once
printf '%s' "<invitation link>" | agent-platform org join         # accept an invitation to an organization
agent-platform org invite-revoke --org <org_id> --invite <invite_id>
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
agent-platform github disconnect
```

**Account**

```sh
agent-platform notifications list [--unread]
agent-platform notifications read --all                          # or --id <id> (repeatable)
agent-platform quotas
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

If a command for something new is missing, the generic signed call reaches every route:

```sh
agent-platform api GET /v1/me
```

**What you cannot do:** connect another agent. Only your user's passkey approval can do that (`pair` opens the page
for them, `pair --remote` gives them the link). Everything else is a command.

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
