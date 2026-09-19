---
name: cost-audit
description: >-
  Audit an AWS account for wasted spend and write a savings report. Use when the user asks
  to audit AWS costs, find wasted spend, reduce the AWS bill, run a FinOps review, or says
  "why is our AWS bill so high", "where is our money going", "find idle resources", "our
  costs jumped this month", or "clean up unused AWS resources". Covers Cost Explorer
  trends, orphaned EBS volumes and Elastic IPs, idle NAT gateways and databases, and S3
  storage lifecycle gaps.
argument-hint: "[profile name, or what to investigate]"
allowed-tools:
  - Read
  - Write
  - Glob
  - AskUserQuestion
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/"*)
  - Bash(aws sts get-caller-identity*)
  - Bash(aws configure list-profiles*)
model: inherit
user-invocable: true
---

# AWS cost audit

Audit an AWS account for wasted spend: **$ARGUMENTS**

## Before anything else

Four things happen before the audit starts, in this order. Do not reorder them and do not
skip ahead because the user sounds in a hurry.

**1. Ask which AWS profile to use. Always. This is the first thing you do.**

Before any AWS call, before reading any file, before planning anything — establish the
profile. List the real ones:

```
aws configure list-profiles
```

Then ask with `AskUserQuestion`, offering those actual profile names as the options.

**Ask even when `$ARGUMENTS` appears to name a profile** — present it as the preselected
option and let them confirm. **Never fall back to the default profile silently.** A
default that happens to be production is exactly how the wrong account gets audited.

Then confirm what those credentials actually are, and say it out loud:

```
aws sts get-caller-identity --profile <PROFILE>
```

Report the account ID and the ARN before going further. Production and sandbox profiles
sit next to each other in the same config file with names that differ by three
characters; the account number is the thing that cannot be misread.

Only once the user has confirmed the profile and account do you run anything else.

**2. Run the preflight check.** It costs nothing and answers two questions at once:
which permissions these credentials actually hold, and how they authenticated.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/preflight.py" --profile <PROFILE> --region <REGION>
```

Report what it says. A denied action disables a specific check, and it is better to know
that now than to hand over a report with a silent hole in it. To grant exactly what is
missing, point them at `${CLAUDE_PLUGIN_ROOT}/iam/cost-audit-role.yaml` (CloudFormation,
creates a role) or `${CLAUDE_PLUGIN_ROOT}/iam/cost-audit-policy.json` (the 20 actions, to
attach directly).

**3. Prefer an assumed role over static keys.** If preflight reports `user` or `root`
credentials rather than `role`, say so once, plainly, and offer the fix:

> You are running as an IAM user with long-lived keys. A least-privilege role is safer —
> short-lived credentials that can only do this one read-only job. The template in this
> plugin's `iam/` directory sets one up, and the stack outputs a ready-made
> `[profile cost-audit]` block for `~/.aws/config`.

Then **continue with the audit if they want to continue**. This is a recommendation, not
a gate: the scripts are read-only, so running them with broader credentials changes
nothing and damages nothing.

If they mention they have already run audits this way, do not treat it as an incident and
do not lecture. Nothing was modified. Say it is worth switching for future runs, because
a least-privilege role means an audit can never do more than audit, and move on. If the
keys were static and used from a shared machine, rotating them afterwards is a reasonable
precaution worth one sentence.

**4. Tell them Cost Explorer is not free.** Each Cost Explorer API request bills roughly
**$0.01**. A normal audit makes a handful — cents, not dollars — but the user should be
told before you spend their money rather than after. One line is enough:

> Cost Explorer bills about $0.01 per request; this audit will make a few. Responses are
> cached locally so re-runs are free.

The resource sweeps (`idle_resources.py`, `s3_storage.py`) use EC2, S3, and CloudWatch
APIs, which are free. Only the Cost Explorer script carries a charge. `preflight.py`
skips its four Cost Explorer probes unless asked, for the same reason.

## The scripts

All four live at `${CLAUDE_PLUGIN_ROOT}/scripts/` and share `--profile`, `--region`,
`--json`, `--no-cache`, and `--cache-ttl`. **Every AWS call they make is read-only** —
no script creates, changes, tags, or removes anything. Say so if the user hesitates.

Run them with `--json` when you want to reason over the numbers, without it when you want
output to quote into the report.

**0. What you are allowed to do** (free, run this first):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/preflight.py" --profile <PROFILE>
```

Exits non-zero if any probed action is denied. `--include-cost-explorer` also probes the
four `ce:Get*` actions, which bills about $0.04.

**1. Where the money goes:**

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cost_explorer.py" --profile <PROFILE> --months 6
```

Monthly totals, spend by service, and month-over-month movers. Start here — it tells you
which of the later sweeps is worth the time. Useful variants:

- `--group-by LINKED_ACCOUNT` in an organization, to find which account is responsible.
- `--group-by TAG --tag-key Environment` to test whether tagging is good enough to
  attribute cost at all. If most spend lands in `(untagged)`, that is a headline finding
  in its own right: nobody can allocate what they cannot see.
- `--recommendations` for AWS's own rightsizing, Savings Plans, and RI advice.
- `--exclude-credits` when credits are masking real usage growth.

**2. What is running and shouldn't be:**

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/idle_resources.py" --profile <PROFILE> --region <REGION>
```

Unattached volumes, unassociated Elastic IPs, stopped instances still paying for storage,
stale snapshots, idle NAT gateways, load balancers with no healthy targets, databases with
no connections. Add `--all-regions` to sweep everywhere — slower, and worth it, because
forgotten resources hide in regions nobody looks at.

**3. What is stored and shouldn't be:**

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/s3_storage.py" --profile <PROFILE>
```

Per-bucket size and storage-class mix from CloudWatch metrics, plus lifecycle gaps:
no policy at all, versioning with no expiry, no multipart-abort rule.

## Reading the output honestly

The savings numbers are **estimates from us-east-1 list prices**, built for ranking
findings against each other. They are not a forecast. Three places they mislead, which you
must carry into the report rather than quietly passing on:

- **Snapshots are incremental.** The script prices them at full volume size, which
  overstates the saving, often by a lot. Say so when snapshots rank highly.
- **Idle RDS shows storage cost only.** Compute is the larger number and depends on the
  instance class. The real saving is bigger than displayed.
- **Versioning and multipart findings show no number** because the wasted bytes are
  genuinely invisible without an S3 Inventory report. Report them as "unknown, likely
  material" rather than as zero.

And treat every finding as a hypothesis until you have checked the obvious innocent
explanation:

- A load balancer with no healthy targets may front an autoscaling group at zero.
- An idle database may be a deliberate warm standby or DR replica.
- An unattached volume may be a deliberate backup, days before a planned restore.
- A NAT gateway passing no traffic may exist for a failover path.

Never present a finding as certain waste when a five-second check would settle it. Where
you can settle it from tags, names, or the repo, do that first. Where you cannot, say
explicitly what the user needs to confirm.

## Write the report

Structure lives in `references/report-structure.md`. Read it and follow it.

Default path: `aws-cost-audit-<account-id>-<YYYY-MM-DD>.md` in the current working
directory. If the user named a path, use it. If the file exists, ask before overwriting.

Two rules about the content:

- **Rank findings by estimated monthly saving**, largest first, and put the total at the
  top. An audit that buries a $400/month finding under six $3 findings has failed.
- **Every finding carries its evidence and its exact remediation command.** A reader
  should be able to verify a claim and act on it without coming back to you.

## Never run the remediation

The report contains `delete-volume`, `release-address`, `terminate-instances` and similar
commands. **You do not run them.** The scripts are read-only by construction, and the
plugin's tool permissions do not allow mutating AWS calls.

If the user asks you to act on a finding, that is a separate decision requiring separate
confirmation, outside this skill. Destructive AWS operations deserve a deliberate, explicit
step — not a step that happens because a report recommended it.

## The order, condensed

1. Ask which profile. Always, first, before anything else.
2. Confirm the account ID and ARN out loud.
3. Preflight the permissions; report denials and credential type.
4. Warn that Cost Explorer bills per request.
5. Run the sweeps.
6. Check each finding against its innocent explanation.
7. Write the report.

## Reporting back

Three or four lines: the path, the total estimated monthly saving, the single largest
finding, and anything that needs the user to confirm before it can be trusted. Do not
summarize the whole report back to them.
