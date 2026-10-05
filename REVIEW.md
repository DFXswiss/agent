# Review

This file is binding for every change and for the review that A38 requires.
`CONTRIBUTING.md` is binding too. A review reads both files and does not change any files.

## Use what already exists

A new endpoint, function, user-interface control, error text, clock, or permission check is allowed only when no existing element in this repository does the same job. Otherwise the change uses the existing element.

The same job means the same behavior for the caller. An existing element whose job is different is not a substitute. Do not force the change into that element. The review names the existing element it considered and why that element's job is different.

## Deviation

A change that does not use an existing element for the same job is a defect, unless the pull request states why that element cannot do the job. The reason stands in the pull request body. A missing reason, an empty reason, or "not relevant" is not a deviation. Setting the finding aside does not remove the defect.

A contradiction of `CONTRIBUTING.md`, or of a document that `CONTRIBUTING.md` names as binding, is a defect on the same terms.

## What the review reports

Each pass lane is read-only. Its prompt contains the sentence A38 requires, which names this file and `CONTRIBUTING.md` and forbids changing files. A defect is a violation of either file that the pull request does not justify. Zero defects means no such violation remains.
