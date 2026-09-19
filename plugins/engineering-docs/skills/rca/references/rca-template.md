# RCA template

Section order for a blameless root cause analysis. Keep the headings as written.

---

## Title

`# RCA: <short incident description>`

Metadata line: incident date, severity, duration, author, status (`Draft` / `Reviewed` /
`Actions tracked`).

## Summary

Four sentences at most: what broke, who it affected, for how long, and what the root cause
turned out to be. Written so an executive who reads nothing else is not misled.

## Impact

Concrete and bounded:

- **Who** — user segments, internal teams, downstream services.
- **How many** — a count or a defensible estimate. If unknown, say "unknown" and add an
  open question about why it is unknown, because that is usually a gap in instrumentation.
- **How long** — from first impact to full resolution, not from detection.
- **What it cost** — revenue, SLA credits, engineer-hours, or reputational cost where it
  can be stated honestly. Omit rather than invent.

## Detection

How the incident was noticed, and how long after first impact. If a human noticed before
the monitoring did, say so plainly — it is one of the most actionable findings a postmortem
can produce.

## Timeline

A table: time (with timezone), event, source of the evidence.

Start before the trigger — include the change that introduced the condition, even if it
landed days earlier. End at full resolution, not at mitigation.

Mark clearly what is precise (log timestamps, alert times) and what is reconstructed from
memory. Mixing the two without saying so is how timelines become fiction.

| Time (UTC) | Event | Source |
|---|---|---|
| | | |

## Root cause

The condition that made this incident possible, and the five-whys chain that reached it.
Branch the chain where there were genuinely several independent causes.

Distinguish explicitly:

- **Trigger** — the proximate event.
- **Root cause** — the underlying condition.
- **Contributing factors** — what made it worse, slower to detect, or slower to fix.

## Contributing factors

Each as its own short subsection with the evidence for it. Include absent controls: the
missing test, the alert that did not exist, the runbook that was stale, the dashboard
nobody was watching. Absences are findings.

## What went well

Genuinely — not filler. Fast detection, a rollback that worked, a runbook that held up,
good escalation. This section tells you which investments are paying off and should be
extended elsewhere.

## What was luck

The part most postmortems skip, and the most valuable section in the document. What made
this less bad than it could have been, that you cannot count on next time? Off-peak timing,
someone happening to be online, a cache that happened to be warm, a customer who happened
to be patient. Each item here is an argument for a corrective action.

## Corrective actions

| # | Action | Owner | Due | Ticket | Prevents recurrence? |
|---|---|---|---|---|---|
| | | | | | |

Sorted by whether the action would have prevented or contained *this* incident, not by
ease. The last column is yes / reduces impact / detection only — be honest about which.

No action should be a variant of "be more careful". If that is all that is left, the
analysis has not reached the root cause yet.

## Open questions

What is still unknown, and who would need to answer it. Unknown impact numbers go here.

## Appendix

Relevant log excerpts, graphs, queries used in the investigation, and the commit range
examined. Enough that someone could redo the analysis.
