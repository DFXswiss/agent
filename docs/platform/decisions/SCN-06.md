# SCN-06 – decisions (agent side of the M2d scenarios E2E-25/26)

The scenarios live in agent-core (`e2e/platform/`, `docs/platform/decisions/SCN-06.md` there).

1. **Golden bundle and model double**: running E2E-26 showed the same two defects SCN-04 had found and fixed on its
   branch (not merged yet): the MCP tool schemas `contracts/mcp/tools/` were missing in the golden image (no session
   could list a platform tool), and the model double did not know the runner's `GET /v1/key` spend poll. This branch
   carries SCN-04's two commits unchanged (identical patches, so SCN-04's rebase drops them) instead of a second fix.
2. **Bundle test**: added a check that the checkout bundle holds the input and output schemas of every guest MCP tool
   (`mcp_tools/*.py`) and none of the top-level non-schema directories.
3. **Handed over to SCN-04** (review): `GET /v1/key` of the model double accepts any bearer token; it should compare it
   with the platform key the double was configured with.
