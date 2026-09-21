---
name: sagemaker-cost-analysis
description: >-
  Generates an interactive SageMaker cost deep-dive dashboard as a self-contained HTML file,
  attributing spend to individual Studio spaces, their human owners, each day, and the
  notebooks that actually ran — weighted by real cell executions and kernel time — including a
  profile of what those notebooks import and read.
  Use whenever the user asks about SageMaker, Studio, JupyterLab, Unified Studio or notebook
  costs, or asks who is driving them. Triggers on: SageMaker cost, Studio cost, notebook cost,
  JupyterLab cost, per-notebook cost, cost per day per notebook, which notebook cost the most,
  who is spending on SageMaker, which notebook is burning the instance, cost per notebook per day, ml.m7i / ml.m5 instance hours, Studio space cost, Unified
  Studio spend, SageMaker FinOps, idle notebook waste, "why is SageMaker so expensive".
argument-hint: "[period, e.g. 'last 3 months', 'Jun-Aug 2026' or '2026-06-01:2026-09-01']"
allowed-tools:
  - Read
  - Write
  - AskUserQuestion
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/"*)
  - Bash(aws sts get-caller-identity*)
  - Bash(aws configure list-profiles*)
model: inherit
user-invocable: true
---

# SageMaker Cost Deep Dive

Cost Explorer stops at a line like `USE1-Studio:JupyterLab-ml.m7i.48xlarge — $3,594`. That
is a fact, not
a finding. This skill pushes through to **which space, whose space, which day, which notebook**,
and produces a filterable HTML dashboard.

## Before anything else

Three rules, in this order, before any AWS call:

1. **Ask which AWS profile to use, first.** `aws configure list-profiles`, then `AskUserQuestion`
   with the real names. Never fall back to the default profile silently. Offer the payer profile
   as a second, optional question.
2. **Confirm the account out loud** with `aws sts get-caller-identity --profile [PROFILE]` before
   any other call. Report the account ID and ARN.
3. **Say that Cost Explorer bills about $0.01 per request** before spending the user's money, and
   that a wider `--months` window means more of them. Prefer an assumed least-privilege role over
   static keys — the template is at `${CLAUDE_PLUGIN_ROOT}/iam/cost-audit-role.yaml`.

Every AWS call this skill makes is read-only. One more thing this dashboard carries that the
others do not: **people's names.** Studio spaces resolve to their human owners. Before sending the
file anywhere, be sure the recipient is entitled to see who spent what.

## What it produces

One self-contained `.html` file (no network, no dependencies — emailable) with:

- **Method & coverage banner** stating what is exact and what is allocated, *before* any number.
- **KPI tiles** — attributed cost, priced hours, priced coverage, spaces, owners, files, storage.
- **Global filters** — date range, space, owner, project, instance type, file substring. Every
  change recomputes the tiles, both charts and all three tables.
- **Daily stacked bars** with a **Space / Notebook toggle**; click a bar to filter to that day.
  Notebook mode carries an explicit *No file signal* residual so both modes total identically.
- **Per-space table** — owner, project, instance-hours breakdown, cost, and a source badge per row.
- **Top-files chart and table** — allocated cost, cell runs, seconds-per-cell, kernel-hours, the
  weighting basis per row, libraries, tables read, cell counts.
- **Day → space → file drilldown** showing each split and its basis, collapsed behind its
  own header so it does not push the tables above it off the screen.
- **Reconciliation table** — billed vs priced hours per day and instance type.
- **Recommendations** — the payer-side settings that would make future periods exact.

## Two numbers, kept apart

Read `references/attribution-method.md` before interpreting or modifying anything. The two
metrics the dashboard reports are not the same thing:

- **Priced coverage** — the share of *billed* hours placed onto a space. A shortfall is real
  spend nobody can be charged for. Target ≥ 98%.
- **Observed / billed hours** — observed runtime against billed runtime. This is the method's
  **error bar**, lands around **103%**, and is *never tuned to 100%*. The overshoot is
  explained: billing starts at `InService` while `CreateApp` fires a minute or two earlier, and
  each occupancy cluster counts its trailing bin whole.

Dollars are always shares of the billed amount — never `hours × rate` — so every daily and
period total reconciles to the bill by construction.

## How per-notebook cost is derived

A space-day's cost is exact. The split **within** it comes from the strongest signal available
for that space-day, and every row records which one it used:

| basis | weight | shown as |
|---|---|---|
| `exec-seconds` | real cell executions × that notebook's own seconds-per-cell | `exact-ish` |
| `exec-count` | real cell executions × the corpus median seconds-per-cell | `est` |
| `editor-sync` | editor open/sync events — measures *open*, not *ran* | `weak` |

Studio logs a `jl-cell-executed` telemetry event per cell run, naming the notebook as an MD5 of
its path, which reverses against the path inventory. A space-day never mixes bases: if it has
executions, editor events are ignored for it, because "open" would dilute "ran".

Seconds-per-cell comes from each notebook's own saved execution metadata, which records only
each cell's **most recent** run — so it profiles the notebook rather than measuring any one day.
Report it as such. `--no-code` drops the S3 read, which demotes every row to `exec-count`.

Weighting by execution *count* alone is not good enough and the dashboard does not do it: on
this account one notebook averaged 144 s/cell over 19 cells while another averaged 1.5 s/cell
over 139, so counts get the heavy-compute notebooks backwards.

## Prerequisites

- `boto3` (`pip install boto3`). Everything else is stdlib.
- Workload-account profile with: `ce:GetCostAndUsage`, `sagemaker:List*`,
  `datazone:ListDomains|ListProjects|SearchUserProfiles`, `cloudtrail:LookupEvents`,
  `logs:StartQuery|GetQueryResults|DescribeLogGroups`, `s3:ListBucket|GetObject`.
- Optional payer/management profile for the cost-allocation-tag, CUR and resource-level checks.
  **These are payer-only settings — a member account cannot read them at any permission level**,
  so their absence is not a permissions bug to chase.

## How to run it

`$ARGUMENTS` is an optional period. Resolve it to `--start`/`--end` (end exclusive), or pass
`--months N` for whole months back from the start of the current month. Default is 3 months.

Ask for the workload profile if the user has not named one. Offer the payer profile too — it is
what turns "no tags are active" from an unknown into a finding.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sagemaker_cost_deepdive.py" \
  --profile [WORKLOAD_PROFILE] --payer-profile [PAYER_PROFILE] \
  --region [REGION] --start [YYYY-MM-DD] --end [YYYY-MM-DD] --open
```

Useful flags:

| Flag | Why |
|---|---|
| `--months 12` | whole-year view; takes a few minutes |
| `--no-code` | skip reading notebooks from S3 (faster; loses libraries/tables) |
| `--no-notebooks` | space level only |
| `--json-out FILE` | the raw collected data, for auditing a number |
| `--bin-minutes` / `--gap-bins` | occupancy resolution and how much quiet stays one session |
| `--top-notebooks N` | how many files to profile from S3 (default 40) |
| `--cache DIR` | notebook parse cache, keyed by S3 ETag (default `.sm_cost_cache`) |

If the payer profile fails to load in boto3 (common for AWS CLI `login_session` profiles), the
script automatically shells out to the `aws` CLI instead and says so. If it cannot be used at
all, the run continues in workload-only mode with a warning — never treat that as fatal.

## Reading the result back to the user

After the run, report:

1. **Period total**, split into runtime / storage / other.
2. **Priced coverage and the observed/billed error bar** — in that order, before any per-space
   number. If priced coverage is below 98%, say which days carry the shortfall.
3. **The top two or three spaces with their owners' names**, and what share of the period each is.
4. **The most expensive files**, with their cell runs, what they import and which tables they
   read — that is the part that tells someone what to actually change. **Quote the weighting mix
   and the "wide error" share alongside them**, both of which the dashboard computes: on the
   validated account a 12-month run came out 72% `exec-seconds` / 23% `exec-count` / 5%
   `editor-sync` with 7% of dollars flagged. Give the ranking, then say plainly how much of it
   is soft. A reader who takes a single file row as a measured cost has been misled.
5. **Note that a file's cost depends on the days it ran**, not only on its own kernel time — it
   shares each space-day with whatever else ran that day. A notebook with more total
   kernel-seconds can still carry less cost than one that ran on more expensive days.
6. **The single most valuable recommendation**, usually activating the DataZone cost-allocation
   tags, noting that tag activation is **not retroactive** so it fixes future periods only.

Then point at the dashboard and mention that filters are global and clicking a bar drills into
that day.

## Scope

Validated end to end for **Studio / Unified Studio JupyterLab** spaces, which is where
notebook spend lives. `CodeEditor`, Unified Studio `Notebook`, classic notebook instances,
training/processing jobs and endpoints are wired into the same usage-type map but **not yet
reconciled against a bill**; when they appear, the dashboard banner marks them unvalidated
rather than implying coverage. `references/attribution-method.md` § Generalising says what each
would need.

## References

- `references/attribution-method.md` — the full method: what is exact, what is allocated, and why
  the error bar sits where it does. Read it before interpreting or changing a number.
- `references/example-report.html` — a real three-month dashboard: 5 spaces, 73 files, 8,215 cell
  executions, with every identifying word in every account, person, space, project, notebook
  path and table name replaced by a neutral one. Owners appear as first names that belong to
  nobody. Open it to see the
  layout, the Space/Notebook toggle and the drilldown before you run anything. The numbers are
  the real ones and reconcile; the names describe nobody.

## Troubleshooting

- **`MalformedQueryException` from Logs Insights** — the window predates the log group or its
  retention. Handled: the occupancy window clamps to the group's lifetime and says so.
- **Priced coverage well under 98%** — usually an app type not reconstructed here, or a domain
  whose logs never reached the group. Check the reconciliation table for the heavy days.
- **Observed/billed above ~112%** — something is double-counted. Look for case-variant space
  names (`Space1` and `space1` are two log streams) and for CloudTrail spans left open.
- **Owners show as UUIDs** — DataZone was unreachable. Studio user profiles are DataZone GUIDs;
  `datazone search-user-profiles` is what maps them to IAM usernames.
- **Everything is `exec-count`, nothing is `exec-seconds`** — the notebook parse cache is
  serving profiles from an older schema. The cache key includes `PROFILE_SCHEMA`; bump it when a
  profile field is added, or delete the cache directory.
- **A notebook ran but shows `est` not `exact-ish`** — its timing profile failed the sample
  gates (`MIN_TIMED_CELLS`, `MIN_SEC_PER_CELL`). Too thin a sample is a reason to fall back to
  the corpus median, not to trust a near-zero seconds-per-cell.
- **A file shows no libraries** — it was not matched in S3. The log path and the S3 key disagree
  when project folders get reorganised; matching is by basename then longest path suffix.
- **Run is slow** — the S3 notebook profiling dominates. Use `--no-code` for a quick pass; the
  parse cache makes re-runs cheap.
