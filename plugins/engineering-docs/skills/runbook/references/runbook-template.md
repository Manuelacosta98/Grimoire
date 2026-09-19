# Runbook template

Section order for an operational runbook. Keep the headings as written — on-call engineers
learn to scan for them, and consistency across a repo is worth more than a better structure
in any single document.

Everything before "Procedure" must be readable in under thirty seconds. The reader is
already stressed.

---

## Title

`# Runbook: <procedure name>`

Metadata line: last verified date, owner (team or rota), and estimated time to complete.
The last-verified date is not decoration — it is how the reader knows whether to trust
this document.

## When to use this

The triggering conditions: the alert name, the symptom, the schedule, the request type.

Equally important: **when not to use this.** Point to the adjacent runbook people reach for
by mistake. This prevents the common failure of someone running a failover procedure for a
problem that only needed a restart.

## Before you start

Everything the reader must have in hand, as a checklist:

- [ ] Access and permissions needed, named specifically — IAM role, VPN, database role,
      production approval, which cloud profile.
- [ ] Tools that must be installed, with versions if it matters.
- [ ] Values to gather first, and where each comes from.
- [ ] Who to notify before beginning, if anyone.

If the reader will discover mid-procedure that they lack something, it belongs here.

## Blast radius

One short paragraph: what this procedure touches, who notices if it goes wrong, and whether
it is reversible. Set expectations before the first command, not after.

## Procedure

Numbered steps. One action per step. For each:

1. **What this step does**, in one line.

   ```
   the exact command, copy-pasteable, with <PLACEHOLDERS> in angle case
   ```

   **Expected:** what the reader should see. Concrete — an exit code, a row count, a
   status string, a log line.

   **If not:** one line, usually a pointer into Known failure modes.

Conventions:

- Placeholders in `<UPPER_SNAKE>`, with a line saying where the value comes from the first
  time each appears.
- A destructive or irreversible step gets a visible warning on the line **before** the
  command, naming what to capture first to make it recoverable.
- The point of no return, if the procedure has one, is its own numbered step that says so,
  with rollback instructions immediately preceding it.

## Verification

How to confirm the whole procedure worked, as distinct from each step working. Usually a
dashboard, a health endpoint, a query, or a synthetic transaction. Include what "healthy"
looks like numerically, and how long to watch before calling it done.

## Rollback

How to undo this, step by step, with the same structure as the procedure.

If it cannot be undone, say so in bold at the top of this section and describe the
forward-fix path instead. "Cannot be rolled back" is a legitimate and important answer —
just never leave the reader to discover it themselves.

## Known failure modes

| Symptom | Likely cause | What to do |
|---|---|---|
| | | |

Drawn from what has actually happened, not from speculation. Each row should be something
someone has really hit.

## Escalation

Who to contact, in order, with the real channel or rota name. Include the threshold: how
long to struggle before escalating, and what to have ready when you do.

Being explicit that escalating early is fine is worth a line. People under-escalate.

## Background

Optional, and last on purpose. Why this procedure exists, how the system works, links to
architecture docs. The 3am reader skips this; the person reading on a calm Tuesday to
understand the system needs it.
