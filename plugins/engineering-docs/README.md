# engineering-docs

Three skills for the documents that matter and never get written, because starting them
from an empty file is the hard part.

```
/plugin install engineering-docs@grimoire
```

| Skill | Invoke | Writes |
|---|---|---|
| `prd` | `/engineering-docs:prd [feature]` | `docs/prd/<slug>.md` |
| `rca` | `/engineering-docs:rca [incident]` | `docs/rca/<date>-<slug>.md` |
| `runbook` | `/engineering-docs:runbook [procedure]` | `docs/runbooks/<slug>.md` |

Each skill follows the same shape: work out what you were given, mine the repo and git
history for everything it can answer itself, ask in one batch about what is genuinely
left, then draft against a template in `references/` and write the file. None of them
overwrite an existing file without asking.

All three accept a file path as the argument — rough notes, a ticket export, an incident
channel transcript, or an existing document to revise.

## What each one is opinionated about

**`prd`** — non-goals, testable requirements, and success metrics. Most weak PRDs are weak
because those three are vague. Requirements are numbered so reviewers can argue with a
specific line. It refuses to invent a success metric you did not give it, and records the
gap as an open question instead.

**`rca`** — blameless, as a matter of accuracy. "Someone ran the wrong command" is not a
root cause, it is where the analysis stopped: the command was destructive with no
confirmation, staging and production were one flag apart, the runbook was stale. Those are
fixable; carelessness is not. It reconstructs the timeline from `git log` over the incident
window, insists on separating trigger from root cause from contributing factors, and
rejects corrective actions that amount to "be more careful". It also includes a *what was
luck* section — what made this less bad than it could have been, that you cannot count on
next time. Most templates omit it; it is usually where the best actions come from.

**`runbook`** — written for a competent engineer who does not know the system, at 3am,
under pressure. Every command copy-pasteable, expected output under each one, destructive
steps flagged before the command rather than after. It takes commands from your
`Makefile`, `justfile`, and CI workflows rather than inventing plausible ones, and
explicitly flags what it could not verify. A fabricated command in a runbook is worse than
a missing one, because the 3am reader will trust it.

## Customizing

The section structures live in `skills/<name>/references/<name>-template.md`. Edit those to
match your house style — the skills read them at runtime and follow whatever is there.
