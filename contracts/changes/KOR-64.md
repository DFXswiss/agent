# KOR-64 – personal message of the requester where access is approved (rule E-28)

Additive contract changes; a package, page, hub or agent tool of before KOR-64 keeps working without a message.

- `log/invitation-package.json`: optional `message` (1–500 characters, the character rules of the agent label,
  `11-agent-access.md` section 3): the inviter's personal message, sealed in the package like `org_name` and shown
  to the invitee where it joins. A package without `message` is exactly as before. Fixture
  `fixtures/log/invitation-package/valid/with-message.json`.
- `vectors/invite.json`: `with_message` (a package with a message and its sealed form under the vector's code and
  nonce); negative `open_package` vectors `package_message_*` (empty, 501 characters, line break, bidi override,
  leading space, not a text).
- The pairing link (`/connect#…`, `/invite#<code>&…`) gains the optional fragment parameter `t` (the agent's
  message, same rules). The fragment never reaches the hub; no API contract changes.
