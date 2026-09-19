---
name: runbook
description: >-
  Write an operational runbook for a recurring procedure. Use when the user asks for a
  runbook, an operational guide, an on-call playbook, a standard operating procedure, or
  says "document how to do this", "write the steps for", "what should on-call do when", or
  "we need this written down before I go on vacation". Also use for deploy, rollback,
  failover, restore, rotation, and scaling procedures.
argument-hint: "[procedure to document]"
allowed-tools: [Read, Write, Glob, Grep, Bash, AskUserQuestion]
model: inherit
user-invocable: true
---

# Runbook

Write an operational runbook for: **$ARGUMENTS**

## Who you are writing for

A competent engineer who does not know this system, at 3am, under pressure, possibly on a
phone. That reader shapes every decision:

- They will not read the whole document first. They will start executing at step 1.
  Anything they must know before starting goes **above** step 1, not buried in step 6.
- They cannot infer. Every command is copy-pasteable, with placeholders in an obvious
  `<ANGLE_CASE>` form and a line saying where each value comes from.
- They need to know after each step whether it worked. A step without a verification is a
  step that fails silently.
- They are frightened of making it worse. Say explicitly which steps are reversible and
  which are not.

## Step 1 — Work out what you were given

Route on `$ARGUMENTS`:

- **Empty** — ask which procedure to document.
- **A file path** — read it. Existing runbook to revise, notes, a shell history, a chat
  transcript from the last time someone did this by hand.
- **Inline prose** — that is the procedure description.

## Step 2 — Take the commands from the repo, not from your imagination

This is the step that separates a useful runbook from a plausible one. **A command you
invented is worse than no command**, because the 3am reader will trust it.

Before writing any step, go look:

- `Makefile`, `justfile`, `Taskfile.yml`, `package.json` scripts, `scripts/`, `bin/` —
  the real entry points.
- `.github/workflows/`, `.gitlab-ci.yml`, or equivalent — CI already encodes the deploy,
  migrate, and rollback procedures, usually correctly and usually more current than any
  prose documentation.
- `docker-compose.yml`, Helm charts, Terraform, CDK — for service names, environment
  names, and the actual resource identifiers.
- Existing docs and prior runbooks for house conventions and environment naming.
- `git log` on the relevant scripts, to check you are not documenting something that was
  replaced last month.

When you cannot find the real command, **mark it** as needing verification rather than
guessing at it. A runbook with three confirmed steps and one flagged gap is honest and
usable. A runbook with four confident steps, one of which is fictional, is a trap.

## Step 3 — Interview for the operational context

Batch into one `AskUserQuestion` call. What the repo will not tell you:

- **What triggers this procedure?** An alert, a schedule, a customer request, a symptom.
  This becomes the "when to use this" section that stops people running it unnecessarily.
- **What access is needed?** Which role, VPN, cloud profile, database credentials, or
  production approval. The reader finding out at step 4 that they lack permission is a
  failure of the document.
- **What is the blast radius if this goes wrong?** Determines how loud the warnings are
  and where the confirmation gates go.
- **Who do they escalate to, and at what point?** With a real channel or rota name.
- **What has actually gone wrong before?** The known failure modes section is only
  valuable if it comes from experience rather than speculation.

## Step 4 — Write steps that can be followed literally

Structure lives in `references/runbook-template.md`. Read it and follow it.

How to write the procedure itself:

- **Numbered, sequential, one action per step.** If a step has an "and" in it, it is two
  steps.
- **Command in a fenced block, on its own.** Never inline in prose. Never several commands
  in one block unless they are genuinely a single unit.
- **Expected output under every command.** What the reader should see. This is how they
  know to continue, and it is the single most commonly omitted element.
- **Then what to do if they do not see it.** One line, usually pointing to the failure
  modes section.
- **Flag destructive steps before the command, not after.** Anything that deletes data,
  drops traffic, or cannot be undone gets a visible warning on the preceding line, with
  what to capture first so the step becomes recoverable.
- **Gate the point of no return.** If the procedure has one, make it a numbered step of
  its own that says so, with the rollback instructions immediately before it.
- **No prose between steps** beyond a short line of context. Save the explanation for the
  background section; the 3am reader is not reading it.

## Step 5 — Write the file

Default path: `docs/runbooks/<slug>.md` in the current working directory. If the repo
already has a runbook directory, use that instead. If the user named a path, use it. If the
file exists, stop and ask before overwriting.

After writing, report: the path, and — this matters — the list of commands you could not
verify against the repo and marked as needing confirmation. That is the user's to-do list
before this document is trustworthy.

## Revising an existing runbook

Runbooks rot faster than any other document, and a stale runbook is actively dangerous.
When revising:

- Check every existing command still exists in the repo. Flag the ones that do not.
- Check environment names, service names, and resource identifiers against current config.
- Preserve step numbering where you can — people reference these by number in incidents.
- Report what you found stale as a list, separately from what you changed.
