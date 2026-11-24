# KOR-63 – Lookup instead of reading everything, short answers, a local MCP server (E-26)

Owner rule E-26 (AI agents are users like people; using the platform is simple and efficient for them), within
E-25 (nothing that only protects and costs time or performance).

1. **One source at the command.** `commands.Tree.add`/`Tree.command` require a one-sentence purpose (the existing
   help text), an `example` and search `words`, optionally the usual next step (`then`) and how `--limit` cuts
   (`trim`). `search`, `--help` (description and epilog) and the MCP tool `search` read only this. There is no
   second list of commands.
2. **The start is the head of the guide.** `guide` prints the guide's text before its first `## ` section (under
   3 500 characters: output, first step per situation, how to look up, `--limit`/`--fields`, MCP); the earlier
   introduction is the section "Secrets and codes". `guide --topic <topic>` prints the matching `## ` sections,
   `--topic all` the whole guide. Topics come from the headings, so a section another package adds later is a topic
   without any change here. No new package file: `guide.md` stays the one shipped text (the package-data entry of
   `pyproject.toml` may not change, legacy test).
3. **The guide is checked against the tree, not generated.** Every command's `example` must be a verbatim part of
   `agents.md` (test), on top of the parity test's rule that the guide has an example of every command. Generating
   the guide would have rewritten its prose; checking keeps it and still prevents drift.
4. **Search without a dictionary.** German and English words are on each command (`words`, German first);
   matching folds case and accents and accepts the same stem (common prefix), weighted name > words > purpose >
   usage. A test requires that each command is found by the first two of its words (top five) and by its own name (first), and
   that every command named in an error's `next` is found first by its name.
5. **`--limit`/`--fields` once, generically.** Applied in `cli.execute` after the handler (`commands.trimmed`) to
   the lists of the result: `first` items, or `last` items and multi-line texts for `task show` and
   `session terminal`; a cut list is named under `limited`, and `next_after` of `task follow` moves behind the last
   event returned. Without the options the output is unchanged.
6. **`no_device` names the command** `device assign` in its `next`, so an agent that meets it finds the step.
7. **Hints on errors from the tree.** An argument error carries `usage` (the usage line of the command it
   happened in) and, for a mistyped command, `did_you_mean` (the closest command names). Both come from the
   command tree, so the rule of `errors.py` (extra fields are never free text from outside) holds. `search` also
   answers an error code it is given (`search no_device`) with that code's `next`.
8. **Paging and `--limit`.** A list the hub pages with `next_before` (audit, notifications, operations) loses that
   cursor when `--limit` cuts it (review finding): the cursor points behind the whole page, so following it would
   skip items. Without `--limit` paging is unchanged.
9. **MCP server local only** (no server at the hub, KONZEPT §5): `agent-platform mcp` runs `cli.execute` in process,
   so keys, rights and the audit marking are those of the tool; `mcp`, `install-connector` and the endless
   `claude watch` are refused through it. Standard library only. Protocol: the 2026-07-28 revision (no
   `initialize`, `server/discover`) and, for current clients that still send `initialize`, 2025-11-25 and older.
   One request at a time: a long `task follow --wait` holds the server (E-25: no concurrency layer).
   `install-connector` keeps the mode of an existing configuration file and follows a link to it.
10. **No one-click bundle (`.mcpb`).** A bundle must contain the server and its dependencies (with the
    platform-specific `cryptography` builds); pointing it at the installed tool would only work on one computer and
    do no more than `install-connector`. A download page at the hub with such a bundle is a proposal in the report.
11. **Not adopted (review, low):** Windows console encoding of the stdio streams (Python 3.12 is the runtime;
    no Windows client tested yet).
