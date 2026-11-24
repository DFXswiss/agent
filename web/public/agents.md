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
uv tool install "dfx-agent[platform] @ git+https://github.com/DFXswiss/agent@develop"
# or: pipx install "dfx-agent[platform] @ git+https://github.com/DFXswiss/agent@develop"
agent-platform guide   # this guide, as shipped with the tool
```

Without `uv` or `pipx`, use a virtual environment (`python3 -m venv ~/.agent-platform-venv`). Do not use `sudo`.

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

**Your user has an invitation link but no account yet:** not available yet – the `register` command is planned. For
now your user opens the invitation link in the browser, registers with a passkey, and then you connect with `pair` as
above.

If `pair` fails with `pairing_timeout`: you must run on the same computer as your user's browser. The
confirmation is handed to you through `http://127.0.0.1` on purpose; there is no code to type in.

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
```

GitHub and the repository list of a workgroup have no command of their own yet (stage c). Until then, ask your user
to open Settings → GitHub once (GitHub itself asks for one confirmation there); an empty repository list allows every
repository of the GitHub connection.

You can do everything your user can do in the browser: tasks and prompts, members and roles (also admins),
workgroups, devices, whiteboards, notifications, credentials, passkeys (removing; adding opens a page for your user),
the recovery code, disconnecting agents. The one thing you cannot do is connect another agent: only your user's
passkey approval can do that (`pair` opens the page for them). `docs/platform/agent-parity.md` lists the command for every action of the web
client. Anything without its own command yet goes through the generic signed call:

```sh
agent-platform api GET /v1/me
agent-platform api POST /v1/tasks --body -      # JSON body on stdin
```

Account-changing actions you take (removing a passkey, replacing the recovery code, disconnecting an agent, changing
roles) are shown to your user as notifications. Act only on your user's instructions. You act only through this tool
and the platform's API; never look for another way around its checks or its audit log.

Where your user is needed (the situation view of the web client's home):

```sh
agent-platform status --attention [--org <org_id>]        # counters, decisions with their questions, stuck tasks
agent-platform task list --org <org_id> --state needs-decision [--mine]   # also blocked, working, done
agent-platform task prompt --task <task_id> --text "Use PostgreSQL."      # answer a question
agent-platform task prompt --task <task_id> --approve [--text "Go ahead."]  # or --decline
```

Answer or approve only what your user decided; never decide for them.

## 4. When something does not work

```sh
agent-platform diagnose                 # account overview with reasons
agent-platform diagnose --task <task_id>
```

`diagnose` answers "why does my task not start?" with a `reason` and a `next` step (for example: no device assigned,
the Claude login expired, the device is offline).

`agent_revoked`: your user disconnected you in the settings, or approved another agent as main orchestrator. Pair
again only if your user asks you to.

## 5. What your user sees

Settings → "Connected agents" shows you as the main orchestrator with your name and computer, when you were
connected and last active, and which agent you replaced. You have full access to the account. Every action you take is in the audit log marked "by agent". Your
user can disconnect you (or all agents) at any time; it takes effect at once.
