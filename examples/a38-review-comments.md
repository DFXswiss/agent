# Review comment examples

Worked comments for the visible text in [docs/a38.md](../docs/a38.md). Each comment below is a complete declaration. A second-run declaration on schema `a38-review/v2` includes the JSON boolean `blocks_merge`. This file is not a second specification.

`EN:` comes first. Each label is its own line. The first review has a title and two sentences in each language. The second review has a title and one sentence: no defects, defects that do not block the merge, or defects that must be fixed. A blank line separates the languages. The title of a pass run names the model, the same text in both languages: `First review with <model>` / `Erster Review mit <model>`, and `Second review with <model>` / `Zweiter Review mit <model>`. That text is the `Model` line of every pass lane in the comment. The guard requires the slot on the comment Ready reads and does not judge the name. An `n_a` run names no model. Two review comments are required before Ready. One review is enough only when the pull request opener is listed in `one_review_authors` in the trusted `.github/pr-guard.json` on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count. Each comment still holds exactly one run. The task sentence and the machine block stay in `<details>`.

The head below is a placeholder. Use the current 40-hex head. When the base revision has no `REVIEW.md`, the prompts are the two sentences shown. When it has `REVIEW.md`, each prompt is the compliance sentence from the specification.

## First review, no defects

This is the comment Ready reads. Post it only when that run found no defects. `N` is how many passes that took.

````
EN:
First review with Grok 4.7
Ready after 2 review passes.
The payment amount is checked against the invoice before it is sent.

DE:
Erster Review mit Grok 4.7
Bereit nach 2 Review-Durchläufen.
Der Zahlungsbetrag wird vor dem Senden gegen die Rechnung geprüft.

<details>
<summary>Details</summary>

Head: `0123456789abcdef0123456789abcdef01234567`
Runs: `conformity-a` with `logic-a`. Two reviews are required.
Lane `conformity-a`:
Provider: xAI
Model: Grok 4.7
Model number: grok-4.7
Result: pass
Prompt:
```text
Review the complete PR diff for conformity with CONTRIBUTING.md.
```
Lane `logic-a`:
Provider: xAI
Model: Grok 4.7
Model number: grok-4.7
Result: pass
Prompt:
```text
Review the complete PR diff for logical correctness.
```
Final result: `passes` 2, `defects` 0.
Set aside: none.

<!-- A38-REVIEW:v1 -->
```json
{"schema":"a38-review/v2","head":"0123456789abcdef0123456789abcdef01234567","passes":2,"defects":0,"set_aside":"none","lanes":[{"id":"conformity-a","result":"pass","status":"complete","provider":"xAI","model":"Grok 4.7","model_number":"grok-4.7","prompt":"Review the complete PR diff for conformity with CONTRIBUTING.md."},{"id":"logic-a","result":"pass","status":"complete","provider":"xAI","model":"Grok 4.7","model_number":"grok-4.7","prompt":"Review the complete PR diff for logical correctness."}]}
```
<!-- /A38-REVIEW:v1 -->

</details>
````

## Second review, no defects

A later comment. Ready requires it together with the first review. One review is enough only when the pull request opener is listed in `one_review_authors` in the trusted `.github/pr-guard.json` on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count. It does not replace the first-run comment, and it does not start CI. The only sentence under the title is `No defects.` / `Keine Mängel.`. `blocks_merge` is false and `defects` is 0. Do not add a sentence about the merge. The sentence about what the pull request changes stays in the details.

````
EN:
Second review with Codex Astra 6
No defects.

DE:
Zweiter Review mit Codex Astra 6
Keine Mängel.

<details>
<summary>Details</summary>

The payment amount is checked against the invoice before it is sent.

Der Zahlungsbetrag wird vor dem Senden gegen die Rechnung geprüft.

Head: `0123456789abcdef0123456789abcdef01234567`
Runs: `conformity-b` with `logic-b`. Two reviews are required.
Lane `conformity-b`:
Provider: OpenAI
Model: Codex Astra 6
Model number: codex-astra-6
Result: pass
Prompt:
```text
Review the complete PR diff for conformity with CONTRIBUTING.md.
```
Lane `logic-b`:
Provider: OpenAI
Model: Codex Astra 6
Model number: codex-astra-6
Result: pass
Prompt:
```text
Review the complete PR diff for logical correctness.
```
Final result: `passes` 1, `defects` 0.
Set aside: none.

<!-- A38-REVIEW:v1 -->
```json
{"schema":"a38-review/v2","head":"0123456789abcdef0123456789abcdef01234567","passes":1,"defects":0,"set_aside":"none","blocks_merge":false,"lanes":[{"id":"conformity-b","result":"pass","status":"complete","provider":"OpenAI","model":"Codex Astra 6","model_number":"codex-astra-6","prompt":"Review the complete PR diff for conformity with CONTRIBUTING.md."},{"id":"logic-b","result":"pass","status":"complete","provider":"OpenAI","model":"Codex Astra 6","model_number":"codex-astra-6","prompt":"Review the complete PR diff for logical correctness."}]}
```
<!-- /A38-REVIEW:v1 -->

</details>
````

## Second review, defects that do not block the merge

One sentence. `blocks_merge` is false and `defects` is an integer >= 1. Each defect is named in the details. The first review stays in place. This comment does not return the pull request to draft, and it does not replace the first-run comment.

````
EN:
Second review with Codex Astra 6
There are defects that do not block the merge.

DE:
Zweiter Review mit Codex Astra 6
Es gibt Mängel, die den Merge nicht blockieren.

<details>
<summary>Details</summary>

The amount is taken from the request, so the caller can underpay. This does not block the merge.

Der Betrag kommt aus dem Request, der Aufrufer kann zu wenig zahlen. Das blockiert den Merge nicht.

Head: `0123456789abcdef0123456789abcdef01234567`
Runs: `conformity-b` with `logic-b`. Two reviews are required.
Lane `conformity-b`:
Provider: OpenAI
Model: Codex Astra 6
Model number: codex-astra-6
Result: pass
Prompt:
```text
Review the complete PR diff for conformity with CONTRIBUTING.md.
```
Lane `logic-b`:
Provider: OpenAI
Model: Codex Astra 6
Model number: codex-astra-6
Result: pass
Prompt:
```text
Review the complete PR diff for logical correctness.
```
Final result: `passes` 1, `defects` 2.
Set aside: none.

<!-- A38-REVIEW:v1 -->
```json
{"schema":"a38-review/v2","head":"0123456789abcdef0123456789abcdef01234567","passes":1,"defects":2,"set_aside":"none","blocks_merge":false,"lanes":[{"id":"conformity-b","result":"pass","status":"complete","provider":"OpenAI","model":"Codex Astra 6","model_number":"codex-astra-6","prompt":"Review the complete PR diff for conformity with CONTRIBUTING.md."},{"id":"logic-b","result":"pass","status":"complete","provider":"OpenAI","model":"Codex Astra 6","model_number":"codex-astra-6","prompt":"Review the complete PR diff for logical correctness."}]}
```
<!-- /A38-REVIEW:v1 -->

</details>
````

## Second review, defects that must be fixed

One sentence. `blocks_merge` is true and `defects` is an integer >= 1. The guard returns a ready pull request to draft. The reason is `Review defects block the merge`. A write hold does not keep Ready. This comment does not replace the first-run comment. The first review stays in place. A newer second-run comment with `blocks_merge` false clears the draft.

````
EN:
Second review with Codex Astra 6
There are defects that must be fixed.

DE:
Zweiter Review mit Codex Astra 6
Es gibt Mängel, die behoben werden müssen.

<details>
<summary>Details</summary>

The amount is taken from the request, so the caller can underpay. This must be fixed before merge.

Der Betrag kommt aus dem Request, der Aufrufer kann zu wenig zahlen. Das muss vor dem Merge behoben werden.

Head: `0123456789abcdef0123456789abcdef01234567`
Runs: `conformity-b` with `logic-b`. Two reviews are required.
Lane `conformity-b`:
Provider: OpenAI
Model: Codex Astra 6
Model number: codex-astra-6
Result: pass
Prompt:
```text
Review the complete PR diff for conformity with CONTRIBUTING.md.
```
Lane `logic-b`:
Provider: OpenAI
Model: Codex Astra 6
Model number: codex-astra-6
Result: pass
Prompt:
```text
Review the complete PR diff for logical correctness.
```
Final result: `passes` 1, `defects` 1.
Set aside: none.

<!-- A38-REVIEW:v1 -->
```json
{"schema":"a38-review/v2","head":"0123456789abcdef0123456789abcdef01234567","passes":1,"defects":1,"set_aside":"none","blocks_merge":true,"lanes":[{"id":"conformity-b","result":"pass","status":"complete","provider":"OpenAI","model":"Codex Astra 6","model_number":"codex-astra-6","prompt":"Review the complete PR diff for conformity with CONTRIBUTING.md."},{"id":"logic-b","result":"pass","status":"complete","provider":"OpenAI","model":"Codex Astra 6","model_number":"codex-astra-6","prompt":"Review the complete PR diff for logical correctness."}]}
```
<!-- /A38-REVIEW:v1 -->

</details>
````

## First review while that run still has defects

The first review is posted only after that run has no defects left. Until then the review states the defects, and nothing else is posted as the first review comment:

```
FINDINGS:
1. src/pay.ts:40 — the amount is taken from the request, so the caller can underpay.
```

After the fix, post the [first review](#first-review-no-defects). `N` is the number of passes until no defects remained. Defects found later by the second review use the two comments above, not this list.

## Not this

The pass-count lines are not the second review. Neither is a mixed pair, nor the old two-sentence defect pair. A second review that found no defects does not add a sentence about the merge.

```
EN:
Second review with Codex Astra 6
There are defects.
They do not block the merge.
```

```
EN:
Second review
Second review after 1 review passes.
No defects, and nothing in this review blocks the merge.
```

```
EN:
Second review with Codex Astra 6
No defects.
Nothing in this review blocks the merge.
```
