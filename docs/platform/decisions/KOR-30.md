# KOR-30 decisions (web client)

1. **Texts only, for the next hub release.** The field keeps its keys (`wg-settings.repositories.*`) and its
   behaviour; label, hint and the empty view now say that without entries every repository of the GitHub
   connection is allowed (ADR-054). The `repo_not_allowed` error text says the workgroup is limited and names both
   ways out (add the repository, or clear the list). Both locales of the client (`de`, `en`) change together.
2. The runner change (agent: decisions/KOR-30.md) works without this release; until it is deployed the deployed client
   still shows "No repository is allowed." for an empty list, which is then only a wrong text.
