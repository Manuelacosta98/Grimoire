# aws-cost-audit

Find out which job spent the money, then write it up for whoever has to sign off on
fixing it.

```
/plugin install aws-cost-audit@grimoire
/aws-cost-audit:glue-cost-analysis [profile]
/aws-cost-audit:sagemaker-cost-analysis [period]
/aws-cost-audit:cost-report [profile] [period]
```

The two deep dives start where Cost Explorer stops. "AWS Glue went up $1,108" is a fact,
not a finding; both push through to the thing you can actually change, and prove the
answer reconciles against the bill.

**`glue-cost-analysis`** attributes billed DPU-hours to individual jobs, reports the
coverage it achieved rather than hiding it, and — where jobs were deleted inside the
period and took their run history with them — recovers their runs from CloudTrail and
CloudWatch Logs. Output is one self-contained HTML file: reconciliation, findings, a full
job inventory, and a page per job with its run history, triggers, config and errors.

**`sagemaker-cost-analysis`** attributes Studio spend to individual spaces, their human
owners, each day, and the notebooks that actually ran — weighted by real cell executions
and kernel time, with the weighting basis recorded per row so a soft number is visibly
soft. Output is one self-contained, filterable HTML dashboard.

**`cost-report`** is the one you send to somebody. It turns a period of Cost Explorer data
into a `.docx`: period trend, the services that actually matter in *this* account with a
root cause each, what moved and why, recommendations split into 0–30 days, 1–3 months and
3 months out, and an Evidence section of Cost Explorer links that open pre-filtered to the
report period. It decides whether credits are even worth mentioning instead of cluttering
the document with a $0.10 adjustment, and when one service dominates it opens a deep dive
on that service rather than restating the number in a longer sentence.

Each skill ships a **deidentified example of its own output** under
`skills/<skill>/references/` — `example-report.html` for the two dashboards,
`example-report.docx` for the report. Each is a real one with every account, job, cluster,
person, bucket, notebook path and error message replaced by a neutral word, figures
included. Names built only from generic infrastructure words survive as they are, because
they name nobody. Open one first — it is quicker than reading this.

## Permissions: use a least-privilege role

Deploy the role before your first run rather than pointing broad credentials at it:

```
aws cloudformation deploy \
  --template-file iam/cost-audit-role.yaml \
  --stack-name grimoire-cost-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides TrustedPrincipalArn=arn:aws:iam::ACCOUNT_ID:root
```

The stack outputs a ready-made `[profile cost-audit]` block for `~/.aws/config`. For an
organization, deploy it as a StackSet across member accounts and audit each by profile.

Rather attach the permissions to something that already exists? `iam/cost-audit-policy.json`
is the same 39 actions as a standalone document.

**Why a role and not access keys.** Static IAM user keys do not expire and are the most
commonly leaked AWS credential — committed to repos, left in shell history, pasted into
CI settings. An IAM user also usually carries far more rights than this audit needs, so
a read-only audit ends up running as a principal that could delete production. An assumed
role hands out short-lived credentials scoped to exactly these 39 read-only actions, and
access is revoked by editing one trust policy rather than rotating keys everywhere.

If you have already run audits with broader credentials, nothing was damaged — the
scripts cannot modify anything. Switch for future runs, and rotate the keys if they were
static and used from a shared machine.

### Check before you run

```
python3 scripts/preflight.py --profile you
```

Reports which of the 39 actions your credentials actually hold, and flags when you are
authenticating as an IAM user or root rather than an assumed role. Exits non-zero if
anything is denied, so it works as a gate. Free by default — add
`--include-cost-explorer` to probe the four `ce:Get*` actions too, which bills about
$0.04.

Knowing up front beats discovering mid-run: a denied action silently disables a whole
class of finding, and a report with an unannounced hole in it is worse than no report.

## Read-only, by construction

Every AWS call is a `get_*`, `describe_*`, `list_*`, `lookup_*`, or `search_*`. Nothing
here creates, changes, tags, or removes a resource. CI greps for mutating calls and fails
the build if one appears — once for boto3 method names, and once for the `aws` CLI
operations the two deep-dive scripts shell out to, since a grep for boto3 would not see
those. So this stays true as the plugin grows.

One grant does reach object contents, and only that one: `s3:GetObject` and
`s3:ListBucket`, scoped by bucket name to the Studio notebook mirror the SageMaker deep
dive profiles and the CloudTrail archive the Glue reconstruction reads. Nothing else here
opens an object.

`idle_resources.py` hands you the `delete-volume` and `release-address` commands. Running
them is your decision, deliberately kept as a separate step outside these skills.

## Cost Explorer is not free

Each Cost Explorer API request bills roughly **$0.01**. A normal run makes a handful.
Responses are cached to local disk (`--cache-ttl`, default 6 hours), so re-running while
you read the report costs nothing. The resource sweeps use EC2, S3, and CloudWatch, which
are free.

Two other things the deep dives can spend. CloudWatch Logs Insights, which the SageMaker
attribution uses for occupancy, bills per GB scanned — small for a three-month window,
worth knowing about for a twelve-month one. And `glue-cost-analysis --archive-bucket`
reads the CloudTrail S3 archive with S3 Select, a few cents of scan; it is opt-in and
only needed for windows wider than CloudTrail's 90-day event history.

## Scripts

Usable directly, without a skill. All take `--profile` and `--region`. The four sweep
scripts share `--json`, `--no-cache` and `--cache-ttl`; the two deep dives take `--start`,
`--end` and `--out` instead, since their output is a dashboard rather than a table.

### `preflight.py`

```
preflight.py --profile you
preflight.py --profile you --include-cost-explorer --json
```

Permission and credential check. See above.

### `cost_explorer.py`

```
cost_explorer.py --profile you --months 6
cost_explorer.py --profile you --group-by LINKED_ACCOUNT
cost_explorer.py --profile you --group-by TAG --tag-key Environment
cost_explorer.py --profile you --recommendations
```

Monthly totals with month-over-month change, spend by service or account or region or tag,
and the biggest movers between the last two months. `--recommendations` adds AWS's own
rightsizing, Savings Plans, and Reserved Instance advice.

Grouping by tag is the quickest way to find out whether you can attribute your own spend.
If most of it lands in `(untagged)`, that is a bigger finding than anything below.

### `idle_resources.py`

```
idle_resources.py --profile you --region us-east-1
idle_resources.py --profile you --all-regions --min-savings 5
```

Sweeps for unattached EBS volumes, unassociated Elastic IPs, stopped instances still
billing for storage, snapshots older than `--snapshot-age` days, NAT gateways passing
almost no traffic, load balancers with no healthy targets, and RDS instances with no
connections over `--idle-days`.

`--all-regions` is slower and worth it. Forgotten resources hide in regions nobody looks
at.

### `glue_cost_report.py`

```
glue_cost_report.py --profile you --out glue.html
glue_cost_report.py --profile you --start 2026-01-01 --end 2026-04-01 --reconstruct
```

Reconciles billed Glue DPU-hours against every run `GetJobRuns` returns, per job, and
writes the dashboard. Defaults to the last 90 complete days, which is also how far back
CloudTrail Event history reaches.

Read the coverage it prints before anything else. At or above 90%, you are done. Below
it, jobs were deleted inside the period — their cost stays on the bill while their runs
vanish from the API — and `--reconstruct` rebuilds them from CloudTrail and CloudWatch
Logs. Reconstructed runs are labelled `ESTIMATED`, never `SUCCEEDED`: CloudTrail records
that a run started and was billed, not how it ended.

Coverage above 100% on any day is a bug, not rounding. The script says so rather than
clamping it.

### `sagemaker_cost_deepdive.py`

```
sagemaker_cost_deepdive.py --profile you --months 3
sagemaker_cost_deepdive.py --profile you --start 2026-06-01 --end 2026-09-01 --no-code
```

Attributes Studio spend to spaces, owners, days and notebooks. Dollars are always shares
of the billed amount, never `hours × rate`, so every total reconciles to the bill by
construction; the hours figure is reported separately as the method's error bar and is
never tuned to 100%.

Two numbers, kept apart, and the skill insists on reporting them in this order: priced
coverage (the share of billed hours placed onto a space, target ≥ 98%) and observed over
billed hours (the error bar, which lands around 103% for explainable reasons).

Needs `datazone:SearchUserProfiles` to turn Studio's user GUIDs into people. Without it
the dashboard still works and owners show as UUIDs.

### `s3_storage.py`

```
s3_storage.py --profile you --min-gb 10
```

Per-bucket size, object count, and storage-class mix, taken from CloudWatch daily metrics
rather than by listing objects — free and instant, at the cost of up to 48 hours of
staleness. Flags buckets with no lifecycle policy, versioning with no noncurrent-version
expiry, and missing multipart-abort rules.

That last one is worth knowing about: abandoned multipart upload parts are billed but do
not show up in the console's object listing. Accounts that push large files can carry
years of them.

## Reading the numbers honestly

Savings are estimates from us-east-1 list prices, built to rank findings against each
other. Three places they mislead:

- **Snapshots are incremental.** Pricing them at full volume size overstates the saving,
  often by a lot.
- **Idle RDS shows storage cost only.** Compute is larger and depends on instance class, so
  the real saving is bigger than displayed.
- **Versioning and multipart findings show no number**, because the wasted bytes are
  genuinely unmeasurable without an S3 Inventory report. Unknown, not zero.

And every finding is a hypothesis until the obvious innocent explanation is ruled out. A
load balancer with no healthy targets may front an autoscaling group at zero. An idle
database may be a deliberate warm standby. An unattached volume may be a backup taken
days before a planned restore.

## Sharing a dashboard or a report

Everything these skills produce embeds everything: account ids, job and cluster names,
notebook paths, people, table names, the text of failed runs. Treat one as confidential
until you have gone through it: before it leaves the account, replace every account id,
job, cluster, person, bucket, notebook path and table name with a neutral word, and cut
each error message back to its exception class — an error string can carry a table name, a
SQL fragment or a row of the data itself.

Be suspicious of names that look harmless. A notebook called `glm_churn.ipynb` contains
no proper noun and still says what you model and who for. The committed examples were
prepared this way, and are worth a look as a reference for how far it has to go.

## Requirements

`boto3`, and credentials holding the 39 read-only actions in
`iam/cost-audit-policy.json`. Deploy `iam/cost-audit-role.yaml` to get exactly those and
nothing else; `preflight.py` tells you what you are missing.

A missing permission produces a warning and a skipped check, not a failed run — one
denied action should not cost you an otherwise useful audit. That is also why preflight
is worth running first: skipped checks are easy to miss in the output.
