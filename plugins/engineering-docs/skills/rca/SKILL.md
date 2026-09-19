---
name: rca
description: >-
  Write a blameless root cause analysis or postmortem for an incident. Use when the user
  asks for an RCA, a postmortem, an incident report, a five-whys analysis, or says "write
  up what happened", "we had an outage", "document this incident", or "the customer wants
  an RCA". Also use after an incident is resolved and the user wants the timeline,
  contributing factors, and corrective actions captured in a document.
argument-hint: "[incident description, or path to notes]"
allowed-tools: [Read, Write, Glob, Grep, Bash, AskUserQuestion]
model: inherit
user-invocable: true
---

# RCA

Write a root cause analysis for: **$ARGUMENTS**

## The one rule

**This document is blameless.** It describes systems, conditions, and decisions that made
sense given the information available at the time. It never names an individual as a cause.

This is not politeness, it is accuracy. "An engineer ran the wrong command" is not a root
cause — it is where you stopped looking. The real finding is underneath: the command was
destructive and had no confirmation, staging and production were one flag apart, the
runbook was stale, the alert fired into a channel nobody watched. Those are fixable.
A person being careless is not.

Concretely, when writing this document:

- Use roles, not names: "the on-call engineer", "the deploying team", "the reviewer".
- Never use "failed to", "should have", "neglected to", or "human error".
- When an action looks obviously wrong in hindsight, ask what made it look reasonable at
  the time, and write **that** down. Hindsight bias is the main failure mode of RCAs.
- If the user's own notes assign blame, quietly reframe it and keep going. Do not lecture
  them about it — just write the better version.

## Step 1 — Work out what you were given

Route on `$ARGUMENTS`:

- **Empty** — ask what the incident was. You need at minimum: what broke, roughly when,
  and how it was noticed.
- **A file path** — read it. Incident channel export, alert history, your own notes from a
  live debugging session, a draft writeup.
- **Inline prose** — that is the incident summary.

If the incident is still ongoing, say so and offer to write the document once it is
resolved. A postmortem written mid-incident is guesswork, and writing it competes with
fixing the thing.

## Step 2 — Reconstruct what you can from the repo

You can often recover a real timeline without asking. Once you know the approximate
incident window:

```
git log --since="<window start>" --until="<window end>" --pretty=format:"%h %ad %an %s" --date=iso
```

Widen the window a few days on the leading side — the change that caused an incident is
frequently not the change deployed nearest to it. Then:

- `git log -S "<suspicious symbol>"` to find when a specific behavior entered the code.
- Read the diffs of anything in the window that touches the failing path.
- Check for config, migration, dependency, and infrastructure changes, not just
  application code. These cause a large share of incidents and get overlooked because
  people grep for feature commits.
- Look for the absent control: no test covering the path, no alert on the metric, no
  rollback procedure. Absences are contributing factors and belong in the document.

Present what you find as **candidate** contributing factors and have the user confirm
before you write them as established. A confidently wrong RCA is worse than a thin one.

## Step 3 — Interview for what only a human knows

Batch into one `AskUserQuestion` call. What you usually cannot recover from the repo:

- **Impact** — who was affected, how many, for how long, and what it cost. Be precise or
  be explicitly uncertain; "approximately 400 users for 22 minutes" or "unknown, see open
  questions", never a vague gesture.
- **Detection** — how it was noticed. An alert, or a customer email? If a customer told
  you first, that is a finding in its own right and deserves a corrective action.
- **Timeline anchors** — first impact, detection, escalation, mitigation, resolution. Wall
  clock with timezone.
- **What was tried that did not work.** Failed mitigations are the most useful and most
  frequently omitted part of a postmortem. They are what the next person needs.

## Step 4 — Get to the actual root cause

Use five-whys, but apply the two rules that make it work:

1. **Stop when you reach something you can fix**, not at a fixed count. Sometimes it is
   three whys, sometimes seven.
2. **Branch when there are genuinely several causes.** Most real incidents are a chain of
   conditions where removing any one would have prevented it, or reduced the blast radius.
   A single linear chain is often an oversimplification. Write the branches.

Separate them honestly:

- **Trigger** — the proximate event that started it.
- **Root cause** — the condition that made the trigger capable of causing this much harm.
- **Contributing factors** — everything that made it worse, slower to detect, or slower to
  fix.

The trigger is the boring part. The root cause and contributing factors are the document.

## Step 5 — Corrective actions that will actually happen

Each action needs an owner (role or named team), a due date, and a tracking ticket
reference if one exists. Then apply two filters:

- **Sort by whether it would have prevented or contained this incident**, not by how easy
  it is. Easy-and-irrelevant actions are how postmortems become theater.
- **Reject "be more careful" in all its forms** — more training, more review, more
  documentation-as-the-only-fix. If the sole corrective action is that people will try
  harder, the analysis has not finished. Push for the control that makes the failure
  impossible or self-evident.

Prefer, in order: make it impossible, make it fail loudly, make it detected fast, make it
recoverable fast, document it.

## Step 6 — Write the file

Structure lives in `references/rca-template.md`. Read it and follow it.

Default path: `docs/rca/<YYYY-MM-DD>-<slug>.md` in the current working directory, dated
with the incident date, not today's date. If the user named a path, use it. If the file
exists, stop and ask before overwriting.

After writing, report: the path, the root cause in one sentence, and the corrective actions
that still need an owner. Do not summarize the whole document back — they can read it.
