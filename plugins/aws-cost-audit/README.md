# aws-cost-audit

Find money you are spending on nothing.

```
/plugin install aws-cost-audit@grimoire
/aws-cost-audit:cost-audit [profile]
```

The skill confirms which account it is pointed at before spending anything, runs the
sweeps, checks each finding against its innocent explanation, and writes a report ranked
by estimated monthly saving.

## Permissions: use a least-privilege role

Deploy the role before your first audit rather than pointing broad credentials at it:

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
is the same 20 actions as a standalone document.

**Why a role and not access keys.** Static IAM user keys do not expire and are the most
commonly leaked AWS credential — committed to repos, left in shell history, pasted into
CI settings. An IAM user also usually carries far more rights than this audit needs, so
a read-only audit ends up running as a principal that could delete production. An assumed
role hands out short-lived credentials scoped to exactly these 20 read-only actions, and
access is revoked by editing one trust policy rather than rotating keys everywhere.

If you have already run audits with broader credentials, nothing was damaged — the
scripts cannot modify anything. Switch for future runs, and rotate the keys if they were
static and used from a shared machine.

### Check before you run

```
python3 scripts/preflight.py --profile you
```

Reports which of the 20 actions your credentials actually hold, and flags when you are
authenticating as an IAM user or root rather than an assumed role. Exits non-zero if
anything is denied, so it works as a gate. Free by default — add
`--include-cost-explorer` to probe the four `ce:Get*` actions too, which bills about
$0.04.

Knowing up front beats discovering mid-audit: a denied action silently disables a whole
class of finding, and a report with an unannounced hole in it is worse than no report.

## Read-only, by construction

Every AWS call is a `get_*`, `describe_*`, or `list_*`. Nothing here creates, changes,
tags, or removes a resource. CI greps for mutating boto3 calls and fails the build if one
appears, so this stays true as the plugin grows.

The report hands you the `delete-volume` and `release-address` commands. Running them is
your decision, deliberately kept as a separate step outside this skill.

## Cost Explorer is not free

Each Cost Explorer API request bills roughly **$0.01**. A normal audit makes a handful.
Responses are cached to local disk (`--cache-ttl`, default 6 hours), so re-running while
you read the report costs nothing. The resource sweeps use EC2, S3, and CloudWatch, which
are free.

## Scripts

Usable directly, without the skill. All take `--profile`, `--region`, `--json`,
`--no-cache`, and `--cache-ttl`.

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

## Requirements

`boto3`, and credentials holding the 20 read-only actions in
`iam/cost-audit-policy.json`. Deploy `iam/cost-audit-role.yaml` to get exactly those and
nothing else; `preflight.py` tells you what you are missing.

A missing permission produces a warning and a skipped check, not a failed run — one
denied action should not cost you an otherwise useful audit. That is also why preflight
is worth running first: skipped checks are easy to miss in the output.
