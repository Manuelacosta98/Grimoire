# Grimoire — authoring conventions

This repo is a Claude Code plugin marketplace. The rules below are structural: getting them
wrong produces a plugin that silently fails to load rather than one that errors clearly.

Before every push:

```
claude plugin validate . --strict
claude plugin validate plugins/engineering-docs --strict
claude plugin validate plugins/aws-cost-audit --strict
python3 tests/test_cost_audit.py
```

`claude plugin validate` is Anthropic's own validator and the only one this repo relies
on — there is deliberately no custom validation code here to maintain. It needs no
credentials and no network. CI installs it with `npm install -g @anthropic-ai/claude-code`
and runs the same commands.

### What the official validator does not check

Do not assume coverage it does not have. Verified by testing, not by reading docs:

- **It does not recurse.** Validating the repo root checks only `marketplace.json`; the
  JSON report comes back with `"contents": []`. Each plugin needs its own invocation.
- **It does not check that a skill's `name` matches its folder.** Claude Code resolves
  skills by folder, so a mismatch loads the skill under a name nobody expects. This ships
  today in Anthropic's own `hookify` plugin and passes `--strict`. Nothing here catches
  it either — if it ever bites, add a grep to the workflow.
- **It does not check that `${CLAUDE_PLUGIN_ROOT}` references resolve.** A skill pointing
  at a deleted script fails silently at runtime. CI greps for this.
- **It does not enforce the skill-spec naming rules** — kebab-case, description length,
  forbidden characters. Those are conventions, not validated constraints.
- **The `$schema` URL in `marketplace.json` is a 404.** Anthropic has not published the
  schema, so nothing is schema-validating that file with a generic tool; the CLI has the
  rules built in. The line is kept because the official directory ships it too and it
  starts working if the schema ever lands.

Anthropic's `skill-creator/scripts/quick_validate.py` is **not** a usable substitute here.
It implements the narrower portable agentskills.io spec and rejects `argument-hint`,
`model`, and `user-invocable` — all three in live use in Anthropic's own shipped plugins,
and in ours.

## Layout is load-bearing

```
.claude-plugin/marketplace.json     catalog, at repo root, in .claude-plugin/
plugins/<plugin>/
  .claude-plugin/plugin.json        manifest, inside .claude-plugin/
  skills/<skill>/SKILL.md           skills are directories, not loose .md files
  scripts/                          helper executables, at plugin root
```

Two mistakes the validator catches because both are easy to make and invisible at runtime:

- **The manifest goes inside `.claude-plugin/`; everything else goes at the plugin root.**
  A `skills/` directory nested inside `.claude-plugin/` is not found.
- **A skill is `skills/<name>/SKILL.md`**, and the folder name must equal the frontmatter
  `name`. Claude Code resolves skills by folder.

## Version sync

**When changing a plugin, bump the version in both places, to the same value:**

1. `plugins/<name>/.claude-plugin/plugin.json` → `version`
2. `.claude-plugin/marketplace.json` → that plugin's entry → `version`

At install time plugin.json's version wins and the marketplace entry is silently ignored,
so a drift leaves the catalog advertising a version nobody actually receives. This is the
single most common way to break a marketplace, which is why CI fails on it.

While iterating locally, either bump the version each time or clear both caches:

```
rm -rf ~/.claude/plugins/marketplaces/grimoire/ ~/.claude/plugins/cache/grimoire/
```

Clearing only one of the two does nothing.

## Naming

- Kebab-case everywhere: plugin names, skill names, agent names.
- Skills invoke as `/<plugin>:<skill>`, e.g. `/engineering-docs:prd`.
- `name`: `^[a-z0-9-]+$`, 64 characters maximum, no leading, trailing, or doubled hyphen.
- `description`: 1024 characters maximum, and **no angle brackets** — the skill spec
  forbids them. Use `[square brackets]` or `UPPER_SNAKE` for placeholders instead.
- Before adding a plugin, check the name does not collide with one commonly installed
  elsewhere. Two plugins with the same name conflict on a user's machine.

## Writing skills

- **Prefer `skills/<name>/SKILL.md` to the legacy `commands/*.md` layout.** Both load
  identically; the skill directory is the current form and holds bundled resources.
- Keep the SKILL.md body to roughly 1,500–2,000 words. Section structure and templates
  belong in `references/`, loaded only when needed. Do not duplicate content between the
  body and a reference file — put it in one of them.
- The `description` is what decides whether the skill triggers at all. Write it in third
  person and include the phrases a user would really say, not a summary of the feature.
- Reference bundled files as `${CLAUDE_PLUGIN_ROOT}/scripts/<name>.py`. Note that
  `CLAUDE_PLUGIN_ROOT` is the **plugin** root, not the skill root. Never hardcode an
  absolute path or a `~` path. The validator checks every referenced path exists.
- Scope `allowed-tools` as tightly as the skill allows. A skill that only needs to run
  its own scripts should say `Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/"*)`, not
  `Bash`.

## The read-only rule

**`plugins/aws-cost-audit/scripts/` must never call a mutating AWS operation.** Only
`get_*`, `describe_*`, `list_*`, `lookup_*`, and `search_*`. CI greps for mutating boto3
method calls and fails the build if one appears. The pattern requires a leading dot and a
trailing `(`, so it matches `.delete_volume(` but not the CLI strings in remediation
messages (`"aws ec2 delete-volume --volume-id"`) or identifiers like `output_file`.

**The same rule applies to the `aws` CLI, and needs its own guard.** `glue_cost_report.py`
and `sagemaker_cost_deepdive.py` reach AWS by shelling out, where a mutating call is a
plain string — `["glue", "delete-job"]` — that a grep for boto3 method names cannot see.
A second CI step greps for quoted CLI operations beginning with a mutating verb. Two
read operations deliberately fall outside its verb list: `select-object-content`, which
the trail-archive scan uses, and `start-query`, which starts a Logs Insights query. If
either ever appears as a CLI string, argue for it rather than widening the allowlist
quietly.

This is a promise made in the README and in the skill, to people who will point this at
production accounts. The report tells the user what to delete; the user does the deleting.
If a genuine need for a mutating call ever arises, it belongs in a separate plugin with a
name that says so — not quietly added here.

### Credentials: assume a role, do not use static keys

**Every AWS call the scripts or the skill make should run under an assumed, least-
privilege role — not long-lived IAM user access keys, and never account root.**

The reasons, in order of how much they matter:

- **Static keys do not expire.** They are the most commonly leaked AWS credential:
  committed to repos, left in shell history, copied into CI settings. An assumed role
  hands out short-lived credentials that stop working on their own.
- **An IAM user usually carries far more rights than this audit needs.** Running a
  read-only audit as a principal that can also delete production is an unnecessary
  blast radius. The role in `plugins/aws-cost-audit/iam/` grants exactly the 39 read-only
  actions the scripts call and nothing else.
- **A role is auditable and revocable.** You can see who assumed it and when, and cut
  access by changing one trust policy instead of rotating keys everywhere.

The plugin ships what is needed for this:

- `iam/cost-audit-policy.json` — the 39 actions, nothing more.
- `iam/cost-audit-role.yaml` — CloudFormation creating a role with that policy, with an
  optional `ExternalId` for third-party access.
- `scripts/preflight.py` — reports which actions the current credentials hold, and
  **flags when the caller is an IAM user or root rather than an assumed role**. Probes
  aim at names that do not exist (`grimoire-preflight-probe`), so "no such job" proves
  the call was authorised and costs nothing.

**Every action the role grants must be named as a string literal in a script.** CI diffs
three sources — the `"service:Action"` literals in `scripts/*.py`, `iam/cost-audit-policy.json`,
and `iam/cost-audit-role.yaml` — and requires them to be the same set. A script that
reaches AWS through the CLI therefore carries a `REQUIRED_ACTIONS` list naming what it
calls; without one, adding its actions to the policy fails the build. Derive that list
from the call sites, not from the skill's permission table, which documents more than the
code uses. Payer-account-only actions stay out of the role and are written in a comment
without quotes, so the diff does not pick them up.

**If someone already ran the audit with broader credentials**, that is not a crisis and
the skill should not scold them — the scripts are read-only, so nothing was changed. But
say plainly that it is worth switching, and why: the next run costs nothing extra to do
properly, and a least-privilege role means an audit can never do more than audit. Point
them at the template and at the `[profile cost-audit]` snippet the stack outputs. If the
credentials were static keys used from a shared machine, rotating them afterwards is a
reasonable precaution.

Keep this advice proportionate. It is a recommendation for a read-only tool, not a
security incident.

### Example reports must name nobody

The two deep-dive skills each ship `references/example-report.html`: a real dashboard with
every identifying word — account, job, person, space, bucket, notebook path, table name,
error message — replaced by a neutral one. Nothing here enforces that; it is a rule, so hold
to it. Never commit a report that has not had every identifier replaced, and never paste a
raw dashboard in "just to look at the numbers".

Work name by name rather than by pattern: a name is safe only if every word in it describes
infrastructure rather than a company, a person, a product or a line of business. That means
a name built entirely of generic words (`adhoc-snapshot-loader-job`) can stay as it is,
because it names nobody — the docs say so rather than overclaiming. It also means the
obvious substitutions are the ones that get missed: a notebook called `glm_churn.ipynb`
names a model family and a line of business even though neither word is a proper noun.

Free-text error messages are the trap. Cut them back to their exception class rather than
trying to clean them, because an error string can carry a table name, a SQL fragment, an
ARN, or a row of the data itself.

Scripts should also:

- Take `--profile` and `--region`, and never assume the default profile.
- Fail with a readable sentence rather than a traceback. `_common.py` has the helpers:
  `die()`, `explain_client_error()`, `call()`, `try_call()`.
- Degrade rather than abort when one permission is missing — `try_call()` warns and skips,
  so one denied action does not lose an otherwise useful audit.
- Label estimated prices as estimates, and say where they mislead. Confident wrong numbers
  get acted on.
