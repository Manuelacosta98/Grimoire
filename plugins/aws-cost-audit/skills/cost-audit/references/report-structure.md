# Cost audit report structure

Section order for the audit report. The reader is usually someone deciding whether to fund
a cleanup, so the top of the document must answer "how much, and what do I do first".

---

## Title

`# AWS cost audit: <account alias or ID>`

Metadata line: account ID, profile used, regions swept, date, and the analysis window.

## Bottom line

Three or four lines, no more:

- Current monthly spend and its trend over the window.
- Total estimated monthly saving identified.
- The single largest finding, named.
- What needs a human decision before anything can proceed.

Someone who reads only this section should know whether the cleanup is worth scheduling.

## Spend trend

The monthly totals table from `cost_explorer.py`, with month-over-month change.

Then one paragraph on what the trend actually shows. Growth is not automatically a
problem — cost tracking usage growth is healthy; cost growing while usage is flat is not.
Say which one this is, or say you cannot tell and what data would settle it.

## Where the money goes

Top services by spend in the most recent complete month, with each one's share.

Then the movers table, and an explanation of the largest changes. An unexplained
month-over-month jump is the most valuable thing in this document — chase it before
cataloguing small waste.

If cost was grouped by tag and most spend landed in `(untagged)`, say so here explicitly.
An account that cannot attribute its own spend has a governance problem that outranks
every individual finding below.

## Findings

Ranked by estimated monthly saving, largest first. One subsection each:

### F1 — <short title> (~$X/month)

- **What** — the resource or pattern, with identifiers.
- **Evidence** — the specific output that supports it. A reader must be able to verify the
  claim independently.
- **Confidence** — high, or what would need checking. If there is an innocent explanation
  you could not rule out (a warm standby, an autoscaling group at zero, a deliberate
  pre-restore backup), name it here rather than in a footnote.
- **Saving** — the estimate, and where it is unreliable. Snapshots are incremental so
  full-size pricing overstates them. Idle RDS shows storage only, so compute makes the
  real figure larger. Versioning waste is unmeasurable without an S3 Inventory report.
- **Remediation** — the exact command or console path.
- **Risk of acting** — what breaks if this was not actually waste.

Group trivially small findings of the same kind into one entry with a count and a
subtotal. Twenty $2 snapshots are one finding, not twenty.

## Quick wins

The subset that is low-risk, reversible, and needs no coordination — usually unassociated
Elastic IPs and clearly orphaned volumes. Present as a checklist so someone can work
through it in an afternoon.

## Needs a decision

Findings that are real but where acting requires someone to choose: rightsizing that
changes performance headroom, Savings Plans that commit spend for a year, deleting data
with an unclear owner. Each with the trade-off stated plainly and who should decide.

## Structural recommendations

The things that stop this list regrowing, which matter more than any single deletion:

- Tagging policy and enforcement, if attribution was poor.
- Lifecycle policies as a default on new buckets.
- Budget alerts and anomaly detection, if absent.
- Required expiry on snapshots and noncurrent versions.

A cleanup without these is a cleanup you will repeat in six months.

## Method and caveats

State plainly, every time:

- Every AWS call made was read-only.
- Savings are estimates from us-east-1 list prices, for ranking rather than forecasting.
- S3 sizes come from CloudWatch daily metrics and lag by up to 48 hours.
- Which regions were swept — and, if not all of them, that unswept regions may hide more.
- Any check skipped for lack of permissions, naming the IAM action that was denied.

The caveats are not boilerplate. An audit that overstates its precision gets acted on
wrongly, and a single wrong deletion costs more than the whole exercise saved.
