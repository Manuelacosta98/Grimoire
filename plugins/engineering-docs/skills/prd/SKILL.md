---
name: prd
description: >-
  Write a product requirements document. Use when the user asks for a PRD, a product spec,
  a requirements doc, a feature spec, or says "spec this out", "write up what we are
  building", "turn this into a PRD", or "document the requirements". Also use when the user
  pastes rough feature notes, a customer request, or a ticket and wants it turned into a
  structured requirements document with goals, non-goals, and success metrics.
argument-hint: "[feature or problem to spec]"
allowed-tools: [Read, Write, Glob, Grep, Bash, AskUserQuestion]
model: inherit
user-invocable: true
---

# PRD

Write a product requirements document for: **$ARGUMENTS**

A PRD earns its keep by making disagreement visible before code is written. The sections
that do that work are **non-goals**, **success metrics**, and **open questions** — most bad
PRDs are bad because those three are vague or missing. Spend your effort there.

## Step 1 — Work out what you were given

Route on `$ARGUMENTS`:

- **Empty** — ask the user what they want to spec. One question, then continue.
- **A file path** — read it. Rough notes, a ticket export, a transcript, an existing draft
  PRD to revise. If it is already a PRD, you are revising, not starting over: keep its
  structure and section order, fill the thin parts, and say what you changed at the end.
- **A URL or ticket ID** — ask the user to paste the content; do not guess at what it says.
- **Inline prose** — that is the feature description. Use it.

## Step 2 — Mine the repo before asking the user anything

Every question you can answer yourself is a question the user should not have to answer.
Before the interview, spend a few tool calls on:

- `git log --oneline -30` and the repo's README for what this codebase actually is.
- `docs/`, `ADR/`, `rfcs/`, or similar for existing conventions and prior related specs.
  If a prior PRD exists for an adjacent feature, match its section order and voice.
- Grep for the feature's nouns in the codebase. If a partial implementation already
  exists, that changes the PRD from "build this" to "finish this" — a materially
  different document. Say so explicitly in the Problem section.

## Step 3 — Interview for what is genuinely missing

Batch every remaining question into **one** `AskUserQuestion` call. Do not ask serially;
it is tedious and the answers are usually related.

Ask only about things that change the document. The questions that reliably matter:

- **Who is this for?** A PRD for an internal tool used by six people is not the PRD for a
  customer-facing feature, and the difference shows up in every section.
- **What does success look like, numerically?** If the user has no metric, that is itself
  a finding — record it in Open Questions rather than inventing a plausible number.
- **What are we explicitly not doing?** Users often have a strong opinion here and are
  never asked. This is the highest-value question in the list.
- **What is the deadline or forcing function?** Shapes scope and the rollout section.

If the user answers "I don't know" to something, that is a legitimate answer: it belongs
in **Open Questions** with a note on who could decide it. Do not paper over it.

## Step 4 — Draft against the template

The section structure lives in `references/prd-template.md`. Read it and follow it. Do not
restate its contents here or invent a different skeleton.

Rules that matter more than the structure:

- **Write requirements as testable statements.** "The export must complete within 30
  seconds for a 10,000-row dataset" is a requirement. "Exports should be fast" is a wish.
  If you cannot imagine the test, rewrite the line.
- **Number the requirements** (`R1`, `R2`, …) so reviewers can comment on a specific one
  and engineers can reference them in PRs.
- **Separate functional from non-functional.** Latency, availability, data retention,
  access control, and audit logging are where PRDs quietly omit the expensive parts.
- **Mark every assumption as an assumption.** When you inferred something from the repo
  rather than being told it, say so in the line itself. A reviewer skimming should be able
  to spot what you made up.
- **Do not estimate effort or timelines** unless the user supplied them. Engineering
  estimates invented by a document are worse than no estimate.
- **Keep it to two pages of substance.** A PRD nobody finishes reading has failed.

## Step 5 — Write the file

Default path: `docs/prd/<slug>.md` relative to the current working directory, where
`<slug>` is a kebab-case short name for the feature.

- If the user named a path, use it.
- If the file already exists, **stop and ask** before overwriting — show them the first
  few lines of what is there so they know what they would lose.
- If `docs/` does not exist but the repo clearly keeps docs somewhere else, use that
  location instead and mention why.

After writing, report in two or three lines: the path, the number of requirements, and the
open questions that still need a human decision. Lead with the open questions — those are
the reason to read the document.

## Revising an existing PRD

When the input is an existing PRD, the job changes:

- Preserve section order and heading style even if you would have chosen differently.
- Do not silently delete content. If a section should go, say so in your summary and let
  the user decide.
- Track what changed. End with a short list of the sections you added to, tightened, or
  flagged — not a diff, just enough that a reviewer knows where to look.
