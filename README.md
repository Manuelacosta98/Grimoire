# Grimoire

A spellbook of AI skills and agents, forged on live AWS infrastructure. Free to steal.

A [Claude Code](https://claude.com/claude-code) plugin marketplace. Two plugins so far:
one that writes the engineering documents nobody wants to start from a blank page, one
that audits an AWS account for wasted money.

## Install

```
/plugin marketplace add Pompelo/Grimoire
/plugin install engineering-docs@grimoire
/plugin install aws-cost-audit@grimoire
```

To try it from a local clone instead:

```
/plugin marketplace add /path/to/Grimoire
```

## Plugins

### `engineering-docs`

One skill per document type. Each interviews you for what is missing, mines the repo and
git history for what it can work out itself, and writes a finished Markdown file.

| Skill | Invoke | Writes |
|---|---|---|
| PRD | `/engineering-docs:prd [feature]` | `docs/prd/<slug>.md` |
| Root cause analysis | `/engineering-docs:rca [incident]` | `docs/rca/<date>-<slug>.md` |
| Runbook | `/engineering-docs:runbook [procedure]` | `docs/runbooks/<slug>.md` |

Opinions baked in, because they are the difference between a document that helps and one
that gets skimmed once:

- The **PRD** pushes hardest on non-goals, testable requirements, and success metrics —
  the three sections that make disagreement visible before code is written.
- The **RCA** is blameless as a matter of accuracy, not politeness. "An engineer ran the
  wrong command" is where you stopped looking; the finding is underneath it. It also
  includes a *what was luck* section, which most postmortem templates omit and which is
  usually the most valuable part.
- The **runbook** is written for a competent engineer who does not know the system, at
  3am, under pressure. It takes commands from your `Makefile` and CI config rather than
  inventing plausible ones, and flags what it could not verify — because a fabricated
  command in a runbook is worse than a missing one.

### `aws-cost-audit`

Finds money you are spending on nothing, and writes it up ranked by what you would save.

```
/aws-cost-audit:cost-audit [profile name]
```

Three scripts underneath, usable on their own:

| Script | What it finds |
|---|---|
| `preflight.py` | Which of the 20 required IAM actions you actually hold, and whether you are using a role or static keys |
| `cost_explorer.py` | Monthly spend trend, breakdown by service or account or tag, biggest movers, AWS's own rightsizing and Savings Plans advice |
| `idle_resources.py` | Unattached EBS volumes, unassociated Elastic IPs, stopped instances still billing for storage, stale snapshots, idle NAT gateways, load balancers with no healthy targets, databases with no connections |
| `s3_storage.py` | Per-bucket size and storage-class mix, buckets with no lifecycle policy, versioning with no expiry, missing multipart-abort rules |

```
python3 plugins/aws-cost-audit/scripts/preflight.py --profile you
python3 plugins/aws-cost-audit/scripts/cost_explorer.py --profile you --months 6
python3 plugins/aws-cost-audit/scripts/idle_resources.py --profile you --all-regions
python3 plugins/aws-cost-audit/scripts/s3_storage.py --profile you --min-gb 10
```

Every script takes `--profile`, `--region`, and `--json`.

#### Two things to know before you run it

**It is read-only, by construction.** Every AWS call is a `get_*`, `describe_*`, or
`list_*`. Nothing creates, changes, tags, or removes a resource. CI fails the build if a
mutating boto3 call appears anywhere in that directory, so this stays true. The report
hands you the `delete-volume` commands; running them is your decision, deliberately kept
as a separate step.

**Cost Explorer bills about $0.01 per API request.** An audit makes a handful — cents —
but it is your account, so you should know before rather than after. Responses are cached
on local disk, so re-running while you read the report is free. The resource sweeps use
EC2, S3, and CloudWatch, which are free.

Savings figures are estimates from us-east-1 list prices, built for ranking findings
against each other rather than forecasting a bill. The skill is explicit about the three
places they mislead — incremental snapshots, storage-only RDS numbers, and unmeasurable
versioning waste — and about checking the innocent explanation before calling something
waste. A load balancer with no healthy targets might front an autoscaling group at zero.

**Permissions are least-privilege and shipped with the plugin.**
`plugins/aws-cost-audit/iam/cost-audit-role.yaml` is a CloudFormation template creating a
role with exactly the 20 read-only actions the scripts call — verified against the code
in CI, so the policy cannot drift from what the tool needs. Prefer assuming that role
over static IAM user keys: short-lived credentials scoped to one read-only job, revocable
by editing a trust policy.

`scripts/preflight.py` reports which of the 20 actions your credentials hold and flags
when you are running as an IAM user or root rather than an assumed role. Missing
permissions produce a warning and a skipped check rather than a failed run.

## Repo layout

```
.claude-plugin/marketplace.json   the catalog — must be here, at repo root
plugins/<name>/
  .claude-plugin/plugin.json      that plugin's manifest
  skills/<name>/SKILL.md          the nesting matters
  scripts/                        helper executables
tests/test_cost_audit.py          finding logic, against synthetic AWS responses
.github/workflows/validate.yml    official validation + guards, on every PR
```

## Contributing

Validation is Anthropic's own tool, not anything maintained here:

```
claude plugin validate . --strict
claude plugin validate plugins/engineering-docs --strict
claude plugin validate plugins/aws-cost-audit --strict
```

It does not recurse, so the marketplace and each plugin get checked separately. It needs
no credentials and no network. CI installs it with
`npm install -g @anthropic-ai/claude-code` and runs the same commands, plus two `grep`
guards for the things no general validator can know about this repo: that the cost-audit
scripts have stayed read-only, and that every `${CLAUDE_PLUGIN_ROOT}` path a skill
references still exists.

Then the tests:

```
python3 -m pip install boto3
python3 tests/test_cost_audit.py
```

They feed the cost-audit analysis functions fabricated AWS responses, because the scripts
are read-only and their finding paths cannot be exercised against a real account unless
that account happens to be wasteful.

The thing worth understanding is versions. **When you change a plugin, bump the version in
both `plugins/<name>/.claude-plugin/plugin.json` and its entry in
`.claude-plugin/marketplace.json`, to the same number.** At install time plugin.json's
version wins and the marketplace entry is silently ignored, so a drift leaves the catalog
advertising a version nobody receives. `claude plugin validate --strict` fails on it.

See [CLAUDE.md](CLAUDE.md) for the full authoring conventions, including what the official
validator does *not* check.

## License

MIT. Free to steal, as advertised.
