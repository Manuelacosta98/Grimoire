---
name: glue-cost-analysis
description: Attribute AWS Glue spend to individual jobs and produce a self-contained HTML dashboard — reconciles billed DPU-hours from Cost Explorer against every run returned by GetJobRuns, and where jobs were deleted, recovers their runs from CloudTrail and CloudWatch Logs. Ranks jobs by cost, flags legacy duplicates, failure waste and silent resizes, and gives each job its own page with full run history, triggers, config and errors. Use when the user asks "which Glue job is costing us", "why did Glue go up", "attribute our Glue bill", "what is our most expensive ETL job", "Glue DPU-hours by job", or wants a per-job breakdown that Cost Explorer cannot give.
argument-hint: "[aws-profile] [start YYYY-MM-DD] [end YYYY-MM-DD]"
allowed-tools:
  - Read
  - Write
  - AskUserQuestion
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/"*)
  - Bash(aws sts get-caller-identity*)
  - Bash(aws configure list-profiles*)
  - Bash(aws cloudtrail describe-trails*)
model: inherit
user-invocable: true
---

## Before anything else

Three rules, in this order, before any AWS call:

1. **Ask which AWS profile to use, first.** `aws configure list-profiles`, then `AskUserQuestion`
   with the real names. Never fall back to the default profile silently.
2. **Confirm the account out loud** with `aws sts get-caller-identity --profile [PROFILE]` before
   any other call. Report the account ID and ARN.
3. **Say that Cost Explorer bills about $0.01 per request** before spending the user's money. This
   skill makes a handful. Prefer an assumed least-privilege role over static keys — the template is
   at `${CLAUDE_PLUGIN_ROOT}/iam/cost-audit-role.yaml`.

Every AWS call this skill makes is read-only.

## What this skill does

Cost Explorer stops at `SERVICE` and `USAGE_TYPE`. "AWS Glue went up $1,108" is a fact, not a
finding. This skill pushes through to **which job**, and proves the answer reconciles against the
bill.

It produces one self-contained HTML file — no network, no dependencies, emailable — with:

- **Reconciliation header** — billed DPU-hours, DPU-hours attributed to named jobs, coverage %, and
  the residual. Coverage below 90% or above 100% is reported, never hidden.
- **Method diagram** — how each run was priced and which fallback sources were needed.
- **Daily reconciliation chart** — billed vs attributed per day, with anomalous days annotated.
- **Monthly composition**, **top-15 jobs by DPU-hours**, and a **run-duration breakdown** that
  answers whether Glue's 1-minute billing floor is inflating the bill.
- **Findings** the bill cannot show — legacy duplicate jobs, spend burned on failed runs, ungoverned
  ad-hoc jobs, silent resizes, and crawler spend.
- **Full job inventory** — every job with its Glue version, worker, last run and average DPU-hours,
  filterable and sortable.
- **A page per job** — every run on record with its cost, monthly spend, the triggers that start it,
  its configuration, and its most common errors.

The script uses only the Python standard library and shells out to the `aws` CLI.

## Usage

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/glue_cost_report.py" \
  --profile [AWS_PROFILE] [--region us-east-1] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--out PATH]
```

**Start here.** This first pass uses the Glue API alone and is fast (about a minute). Read the
coverage it prints. If it is at or above 90%, you are done — stop and report.

If it comes in below 90%, jobs were deleted inside the period and their runs are gone from the
API while their cost stays on the bill. Recover them:

```bash
#  ... --reconstruct                         CloudTrail Event history (last 90 days) + log spans
#  ... --reconstruct --archive-bucket BUCKET  also the S3 trail archive, for older days
```

`--archive-bucket` is only for windows wider than the default 90 days; find the trail bucket with
`aws cloudtrail describe-trails`. The reconstruction takes 10–20 minutes on a busy account, and the
archive scan reads several GB of trail data (a few cents in S3 Select charges), so both are opt-in —
never run them on an account that already reconciles.

Defaults to the **last 90 complete days** (today excluded — Cost Explorer has not finished posting
it) and `us-east-1`. The script prints a reconciliation summary to stdout and writes the dashboard
to `glue-cost-<profile>.html` in the current directory unless `--out` says otherwise.

That default is deliberate: 90 days is also how far back CloudTrail Event history reaches, so
`--reconstruct` can price every day in the window and `--archive-bucket` is only needed when you
widen the period with `--start`. Ask for an older quarter and the part that predates Event history
degrades — durations there come from whatever CloudWatch log streams survive.

After it runs, open the file and **report the reconciliation summary to the user before any per-job
number** — coverage first, findings second.

## Triggers

Invoke when the user asks any of:

- "which Glue job is costing us the most", "break down our Glue bill by job"
- "why did AWS Glue go up this month", "Glue spend spiked"
- "how many DPU-hours does [job] use", "what does [job] cost us"
- "audit our Glue jobs", "find Glue jobs we can turn off"
- "make a Glue cost dashboard"

Do **not** invoke for a Glue bill that is flat and already understood. This is a multi-step
investigation, not a default section of a cost report. For a period overview across every
service, use `/aws-cost-audit:cost-report`.

## Required IAM permissions

| Permission | Used for |
|---|---|
| `ce:GetCostAndUsage` | billed DPU-hours and the unit rate |
| `glue:GetJobs` | job inventory and configuration |
| `glue:GetJobRuns` | per-run `DPUSeconds`, the authoritative billed figure |
| `glue:ListTriggers`, `glue:GetTrigger` | what starts each job (optional; skipped on AccessDenied) |

For `--reconstruct` additionally:

| Permission | Used for |
|---|---|
| `cloudtrail:LookupEvents` | the true run inventory, and the worker-sizing timeline |
| `logs:DescribeLogStreams` | durations for runs the Glue API lost |
| `s3:GetObject`, `s3:ListBucket`, `s3:SelectObjectContent` | `--archive-bucket` only |

Cost Explorer must be enabled on the account. There can be a 24-hour delay after first enabling it.

## Reading the output

**Coverage is the first thing to report.** The method targets **≥ 90%** of billed DPU-hours attributed
to named jobs.

- **Coverage over 100% on any day is proof of an error, not rounding.** Usually one of two things:
  usage posted by AWS on a different calendar day than the run started (check the neighbouring days —
  the window should reconcile), or a timezone bug. Investigate; never clamp.
- **Coverage under 90%** means jobs were deleted inside the period. `GetJobRuns` only returns runs for
  jobs that still exist, so a deleted job keeps its cost on the bill and loses its history. The
  residual chart will show spikes rather than a flat baseline. Re-run with `--reconstruct`.

When the reconstruction runs, the daily chart stacks by **where each DPU-hour came from** — Glue
API, CloudTrail Event history, CloudTrail S3 archive, and the unattributed remainder. That is the
chart to lead with: it shows the finding and the method's honesty in one read.

Reconstructed runs carry the state `ESTIMATED` and render as **reconstructed**, never as
succeeded — CloudTrail records that a run started and was billed, not how it ended. Their duration
is a per-(job, week) median of CloudWatch log-stream spans plus a validated 14-second startup
offset, and their DPU comes from the CreateJob/UpdateJob timeline. Label them as estimates whenever
you quote them, and give the basis rather than the bare figure — the run count, the DPU used, and
where that DPU came from ("sized from the UpdateJob earlier that day", "no config event survives;
inferred from the billed gap, ±10%").
- **A flat residual is expected and benign** — it is per-run provisioning overhead that `DPUSeconds`
  does not capture.

**Never tune an estimate to reach 100%.** That is curve-fitting. Fix defensible causes only.

## What the reconstruction actually does

Four sources, because one is not enough:

1. **Glue `GetJobRuns`** — per-run `DPUSeconds`, the figure AWS bills. Authoritative; always preferred.
2. **CloudTrail `StartJobRun`** — the true run inventory, swept one UTC day per task. Limited to a
   90-day lookback.
3. **The CloudTrail S3 trail archive** — everything older, filtered server-side with S3 Select.
4. **CloudWatch Logs stream spans** — durations for runs that exist only in CloudTrail. The Glue log
   groups keep retention `never`, so streams outlive the job that made them.

Worker sizing comes from `CreateJob`/`UpdateJob` request parameters built into a **per-job
timeline**, so each run is priced at the configuration in force when it actually ran. Using one DPU
value per job misprices every run on the far side of a resize — by 8× in the case that motivated
this.

Two traps worth knowing, both of which fail silently:

- **Never pass `--max-results` to `aws cloudtrail lookup-events`.** CLI v2 then hands back its own
  pagination token, which `--next-token` does not accept, and every day caps at one page. Let the
  CLI paginate a single day.
- **CloudTrail lookup is about 2 requests/second per account.** Parallelise across days modestly and
  retry with backoff. A throttled day that returns an empty list is indistinguishable from a quiet
  day unless you check.

## Notes

- Crawler DPU-hours (`*-Crawler-DPU-Hour`) are a separate usage type and are reported as a total, not
  attributed per crawler. Interactive sessions likewise.
- Glue retains job-run history for a limited window (roughly 12–15 months in practice). Jobs whose
  runs have aged out show as "never run"; the dashboard says so rather than claiming they never ran.
- Worker sizing is a **timeline, not a value**. A job that was resized mid-period is priced per run at
  the configuration in force for that run, taken from the run record itself.

## References

- `references/service-deep-dive.md` — the full attribution method, its failure modes, and how to
  generalise it to EMR, Lambda, Redshift and SageMaker.
- `references/example-report.html` — a real 90-day dashboard, 195 jobs and 25,938 runs, with
  every identifying word in every account, job, trigger, role, bucket and error message
  replaced by a neutral one. A handful of names built only from generic infrastructure words
  (`adhoc-snapshot-loader-job`) come through unchanged, because they name nobody.
  Open it to see the layout, the filters and the per-job page before you run anything. The
  numbers are the real ones and reconcile with each other; the names describe nobody.
