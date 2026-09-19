# PRD template

Section order for a product requirements document. Keep headings as written so PRDs across
a repo stay skimmable. Drop a section only when it genuinely does not apply, and say so in
one line rather than leaving it silently absent.

---

## Title

`# PRD: <feature name>`

Followed by a metadata line: status (`Draft` / `In review` / `Approved`), author, date, and
a link to the tracking ticket if there is one.

## Summary

Three sentences, maximum. What is being built, for whom, and why now. A reader who stops
here should be able to repeat the gist back correctly.

## Problem

The user-facing problem, stated without reference to the solution. What goes wrong today,
who it happens to, and how often. Quantify where you can — "support fields roughly 15
tickets a week about this" beats "users are frustrated".

If a partial implementation already exists in the codebase, say so here and describe what
it does today. That reframes the document from "build" to "finish", which changes how
every reviewer reads the rest.

## Target users

Who specifically. Named segments or roles, not "users". If there are several, say which is
primary — that decides every trade-off later in the document.

## Goals

What this work must achieve, as outcomes rather than features. Three to five bullets.

## Non-goals

What this work explicitly will not do, and why. This is the most valuable section in the
document: it is where scope disagreements surface while they are still cheap. Include the
things a reasonable reader might assume are in scope.

## User stories

`As a <role>, I want <capability>, so that <outcome>.`

Enough to cover the main flows and the important edge cases. Order them by importance, not
by the order you thought of them.

## Functional requirements

Numbered `R1`, `R2`, … Each one testable — a reader should be able to imagine the test that
proves it. Mark anything inferred rather than confirmed as `(assumption)`.

## Non-functional requirements

Numbered continuing the same sequence. Cover the ones that apply:

- **Performance** — latency and throughput targets, at what data volume.
- **Availability** — uptime expectation, degradation behavior.
- **Security and access control** — who can see and do what.
- **Data** — what is stored, where, for how long, and what happens on deletion.
- **Observability** — what must be measurable once this ships.
- **Compliance** — any regulatory constraint that applies.

Omitting these is how PRDs hide their expensive parts.

## Success metrics

How we will know this worked, measured after launch. Each metric needs a baseline, a
target, and a date to check. If no baseline exists, say that the baseline must be captured
before launch — that is a real requirement.

If the user could not supply a metric, do not invent one. Record it as an open question.

## Rollout

How this reaches users: feature flag, staged percentage, beta cohort, or all at once.
Include what would cause a rollback and who makes that call.

## Open questions

Everything still undecided, each with the person or role who could decide it. A PRD with an
empty open-questions section is usually a PRD that has not been thought about hard enough.

## Appendix

Optional. Prior art, rejected approaches and why, links to research, mockups, or raw notes
the document was built from.
