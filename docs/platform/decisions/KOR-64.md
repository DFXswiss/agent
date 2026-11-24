# KOR-64 decisions (personal message of the requester, rule E-28)

Rule E-28: wherever someone approves a request and so gives an agent or a person access, the requester can add a
personal message that is shown where the approval happens. E-25 applies: only what the rule asks for.

## Inventory: where someone approves a request for access
1. **Pairing of an agent on the same computer** (`/connect`, `pair`, `11-agent-access.md` §5.1): the agent asks, the
   user approves with the passkey. Message: yes.
2. **Pairing of an agent on another computer** (`/connect` with `m=remote`, `pair --remote`, §5.4): same. Message: yes.
3. **Pairing at the registration** (`/invite#<code>&p=…`, `register`, `register --remote`, §5.2, §5.4 step 6): the
   agent asks, the user creates the account and so approves. Message: yes (agent block of the invitation page).
4. **Org invitation** (`/invite#<code>`, `org invite`/`org join`, `06-web.md` §3.1): the inviter asks the person to
   join; the person approves on the intro or existing-account step. The sealed package already carries texts
   (`org_name`, `wg_names`), so the exception of the work package for invitations ("only where a text exists today")
   is met. Message: yes.
5. **Not a place of approval** (no message): direct admission of an existing account (`org admit`, ADR-060 – the
   admin acts, the person does nothing), workgroup grants, platform invitations (no package, no text today),
   org device invitations (the admin pre-approves by creating the code; the device accepts), device requests (a
   user's own request to a device, answered by the device without a person), the certification of a platform device
   (an operator ceremony with the platform root key on the operator's own request).

## Decisions
1. **One rule for the text**: 1–500 characters (code points) under the character rules of the sealed label
   (`11-agent-access.md` §3): no leading or trailing space, none of the Unicode categories Cc, Cf, Zl, Zp, Cs, Co, Cn
   – so one line, no bidi or zero-width tricks. `check_message` (hub reference, runner copy) and `isMessageText`
   (web) implement it; clients trim their input first. Line breaks were left out on purpose: the work package asks
   for the label rules.
2. **Pairing: the link fragment** (`t=<message>`, optional, once). This is the way the name and the computer of the
   agent already travel; the fragment never reaches the hub, so the hub cannot read it, and an older page refuses a
   link with `t` (strict parser) while a link without `t` is unchanged. Not sealed into `label_ct`: the label is
   sealed by the confirming browser and listed later, which the rule does not ask for (E-25).
3. **Org invitation: an optional key `message` in the sealed package** (`04-crypto.md` §4.6). The package is the
   encrypted channel from inviter to invitee that exists today; the hub stores only ciphertext. A package without
   `message` is byte-for-byte as before; an older client that opens a package with a message refuses it
   (`header_invalid`, the strict key set) – accepted, because the web client is served with the hub and the agent
   tool is updated with it; requests without a message keep working everywhere.
4. **Display**: one component (`RequesterMessage`) renders a `figure` with a caption that names whose words these
   are and a `blockquote` with the text as a React text node: no link, formatting or control is ever made from it.
   Nothing is rendered without a message.
5. **Agent tool**: `--message <text>` or `--message -` (stdin) on `pair`, `pair --remote`, `register`,
   `register --remote` and `org invite`. `register` already reads the invitation link from stdin, so `--message -`
   needs the link in `AGENT_PLATFORM_INVITE` (`invalid_argument` otherwise). `org join` prints `message` only when
   the package has one (the output without a message is unchanged). An agent never approves a pairing (§6), so a
   message of a pairing has no agent-side view; the parity table says so.
6. **No hub change**: neither way passes through hub logic; no migration, no API contract change. Contract changes:
   the package schema, its fixture and the shared vectors (`contracts/changes/KOR-64.md`).

## Review
7. Independent review: no high or medium findings. Fixed (low): the `register --remote` row of
   `11-agent-access.md` §8.4 and the `/invite` route row of `06-web.md` §2.4 name the message; the form field has no
   `maxLength` (it counted UTF-16 units, the rule counts characters – the check is `isMessageText`); `04-crypto.md`
   §4.6 says that clients add `message` to user invitations only instead of stating an unchecked rule. Kept (low):
   a blank `--message` is `invalid_argument` in the tool (an explicit option with nothing in it is a mistake), while
   the web form treats an empty field as no message (the field is optional); an invalid message shows the generic
   `params_invalid` of the form (E-25); the shared component stays next to `AgentCode` under `screens/connect`, as
   the invitation screens already import from there.
