---
name: cost-report
description: Write a management-ready AWS cost analysis report as a polished .docx — period trends, per-service insights with root causes, cost drivers, optimization recommendations by time horizon, and clickable Cost Explorer evidence links. Use when the user asks for a cost report, a cloud spend report, a billing report, a quarterly or monthly cost review, a FinOps write-up, or says "make me a cost report", "analyze our AWS bill", "put together a cloud cost summary", "I need to show our AWS spending to management", or "why did our bill go up last month". Also use when the user pastes or uploads raw cost data (a Cost Explorer CSV or JSON export, or numbers in a message) and wants a finished document rather than an answer in chat.
argument-hint: "[aws-profile] [period, e.g. Jun-Aug 2026]"
allowed-tools:
  - Read
  - Write
  - Glob
  - AskUserQuestion
  - Bash(aws configure list-profiles*)
  - Bash(aws sts get-caller-identity*)
  - Bash(aws ce get-cost-and-usage*)
  - Bash(aws ce list-cost-allocation-tags*)
  - Bash(pip install python-docx*)
  - Bash(python3*)
model: inherit
user-invocable: true
---

# AWS cost analysis report

Write the report for: **$ARGUMENTS**

## Before anything else

Three rules, in this order, before any AWS call:

1. **Ask which AWS profile to use, first.** `aws configure list-profiles`, then `AskUserQuestion`
   with the real names. Never fall back to the default profile silently.
2. **Confirm the account out loud** with `aws sts get-caller-identity --profile [PROFILE]` before
   any other call. Report the account ID and the ARN.
3. **Say that Cost Explorer bills about $0.01 per request** before spending the user's money. This
   skill makes a handful. Prefer an assumed least-privilege role over static keys — the template is
   at `${CLAUDE_PLUGIN_ROOT}/iam/cost-audit-role.yaml`.

Every AWS call this skill makes is read-only. The report tells the user what to change; the user
changes it.

**Read `${CLAUDE_PLUGIN_ROOT}/skills/cost-report/references/example-report.docx` before you start.**
It is a real three-month report, deidentified — 10 sections, two deep dives, four figures and eight
working Cost Explorer links. It settles more questions about length, tone and table shape than this
file can.

---

## Where the numbers come from

Establish the data source before planning anything. In order of preference:

**1. Cost Explorer, via the CLI.** Two queries, not one. A single unfiltered query blends credits
into the service rows as negative amounts, and there is then no way to tell a service's real cost
from the credit applied to it.

Gross service charges — credits and refunds excluded at the source:

```bash
aws ce get-cost-and-usage --profile [PROFILE] \
  --time-period Start=[YYYY-MM-01],End=[YYYY-MM-01] \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --filter '{"Not":{"Dimensions":{"Key":"RECORD_TYPE","Values":["Credit","Refund"]}}}'
```

Credits only — invert the filter by dropping the `Not` wrapper. Requires `ce:GetCostAndUsage`;
Cost Explorer can take 24 hours to return data after it is first enabled. The end date is the
first day *after* the last month you want (`End=2026-09-01` covers data through August).

**Add `UsageQuantity` for any service you intend to deep-dive**: `--metrics UnblendedCost
UsageQuantity` with `--group-by Type=DIMENSION,Key=USAGE_TYPE` gives the billed unit count, and
cost ÷ quantity confirms the unit rate. Filter to the billable usage type first — free usage types
(Glue `Catalog-Request`, for one) are millions of units of noise that will wreck any average.

**2. An uploaded CSV or JSON export.** Check whether it was exported with all charge types
included. If credits are mixed in as negative rows, split them out by the `Record Type` column.

**3. Numbers pasted into the conversation.** Ask whether they are pre-credit or post-credit. If the
user does not know, treat them as gross and say so in the report.

**4. Nothing.** Say so, and produce the document with placeholder values for the user to fill in
rather than inventing plausible ones.

Keep the raw API responses. The report's last section promises the reader that every figure is
reproducible; write the responses and the derived CSVs to a folder beside the document, and name
it in the Project Overview table.

---

## Credits: decide whether to mention them at all

Sum every negative amount for the period and compare it against gross spend.

- **Material** — the credit total is at least 1% of gross period spend, *or* at least $50 in any
  single month. Present three cost views (gross, credits, net) in the Cost Breakdown and the
  Executive Summary, add section 4.3, and add the Active Credits Notice callout. Gross is the
  primary view for management reporting, because it is what the infrastructure actually costs when
  the credits stop.
- **Immaterial** — anything below that. **Say nothing about credits anywhere.** No three-view
  tables, no 4.3, no notice, no "gross ≈ net" footnote. A stray −$0.10 adjustment is noise, and
  drawing a reader's eye to it costs you their attention on something that matters.

Where only a single blended dataset exists, any negative line item is a credit regardless of which
service name it sits under — AWS books them under a service, not always under a `Credit` row.

---

## When a service-level answer is not an answer

"AWS Glue rose $1,108" is a fact. It is not a finding, and a reader who can act on it needs the
job, the cluster or the function. Push through to the individual resource when any of these hold:

- one service is more than 25% of period spend, or drives more than 50% of the net change;
- a service spikes and the service-level story would be hand-waving ("more ETL", "more traffic");
- the user asks which job, cluster, team or resource is responsible;
- a service appears or disappears mid-period.

**Try the cheap path first.** If cost-allocation tags are active, group by TAG and stop:

```bash
aws ce list-cost-allocation-tags --profile [PROFILE] --status Active
```

A linked account often answers `AccessDeniedException` — the payer controls tag activation. When
that happens, or when the resources are simply untagged, the cost has to be rebuilt from the
service API, CloudTrail and CloudWatch.

**Read `${CLAUDE_PLUGIN_ROOT}/skills/glue-cost-analysis/references/service-deep-dive.md` before
starting one.** It carries the procedure, the working code, and the failure modes that produce
confidently wrong numbers — one of them was wrong by 8×. Two rules outrank everything else in it:

1. **Reconcile or do not report.** Attribution has to add back up to billed usage at daily
   granularity. Target 90% or better, state the residual, and name what it is made of. Coverage
   above 100% is proof of a bug, not a rounding artifact.
2. **Never tune an estimate to close the gap.** Fix defensible causes only. The moment you are
   choosing a parameter because it makes the totals agree, stop — that is curve-fitting, and the
   number will be wrong in a way no reader can detect.

A deep dive becomes its own numbered section after Cost Breakdown, and renumbers everything after
it. The example report has two of them, on Aurora and on Glue.

---

## What to collect before writing

Ask for whatever is missing:

| Field | Example | Why |
|---|---|---|
| AWS profile | `cost-audit` | Which account. Never guess |
| Project name | `Example Data Platform` | Title line and overview table |
| Account ID | `123456789012` | Cost Explorer links |
| Region | `us-west-2` | Overview table; travels in the link query string |
| Console host hash | `a1b2c3d4` | Account-specific, not derivable. Ask the user to paste one Cost Explorer URL from their address bar and lift it. See `references/cost-explorer-urls.md` |
| Report period | `Jun–Aug 2026` | Complete months only — a partial month reads as a collapse in spend |
| Created by | `Cloud Engineering` | Author line |

Worth asking for, and worth including if offered: optimizations already completed, budget targets,
and whether to add Reserved Instance or Savings Plan commitment analysis.

---

## The sections

Full content spec, table shapes and formatting standards: `references/report-structure.md`.

1. **Project Overview** — metadata table
2. **Executive Summary** — period total, trend, where the movement actually is, key findings
3. **Service-Level Insights** — the services that rank high in *this* account, month by month,
   each with a root cause and one action
4. **Cost Breakdown** — most recent month, historical comparison, credits if material
5. **Cost Drivers & Key Findings** — what moved, why, and what it means
6. **Cost Optimization Recommendations** — short (0–30d), medium (1–3mo), long (3mo+)
7. **Actions Taken** — only changes with a visible signature in the data
8. **Evidence** — Cost Explorer links, the plain-text URL appendix, full service breakdown

Each deep dive is inserted as its own numbered section after Cost Breakdown and renumbers
everything below it. The example report has two, so it runs to ten sections.

Three things go wrong here more than anything else:

- **Section 3 is not a checklist.** Rank the services by this account's own spend and analyze what
  comes out on top. The reference file lists common services as a memory aid for writing root
  causes, not as a roster to fill.
- **Section 8 invites fabrication.** Only assert an action that left a visible drop in the data. An
  instance-class change, a retention tweak or a licence downgrade is invisible at service
  granularity — list those as rows for the team to complete, not as things you established.
- **Every estimate says it is one, inline, with its basis.** "20 DPU inferred from implied 22.8 and
  18.9 on the two heaviest days; ±10%" is useful. A confident wrong number gets acted on.

---

## Building the document

```bash
pip install python-docx --break-system-packages -q
```

Write a self-contained Python script and run it. It should:

- build the full section structure with `python-docx`, following the formatting standards in
  `references/report-structure.md` — heading levels, bordered tables, shaded header rows, bold
  totals, page breaks before the service insights, the cost drivers and the evidence sections;
- render every Cost Explorer link as a **clickable hyperlink** with a short display title ("Open in
  Cost Explorer"), using the `add_hyperlink` helper in `references/cost-explorer-urls.md`. Never
  print a raw URL in the document body — and always repeat every URL verbatim as plain text in the
  Evidence appendix, because some viewers rewrite a hyperlink target containing `#` and land the
  reader on the console home screen;
- separate positives from negatives *before* building any table, if credits were material;
- write to `AWS CLOUD COST ANALYSIS REPORT [PERIOD].docx` in the working directory unless the user
  named a path.

Charts follow the `dataviz` skill. Validate the palette before rendering, and **look at the
rendered PNG before embedding it** — an axis limit that clips the tallest bar is invisible in the
code that produced it.

**Verify the hyperlinks by reading them back out of the finished `.docx`,** not by inspecting the
script that wrote them; there is a five-line assertion in `references/cost-explorer-urls.md`. A
stale helper definition, or a patch applied to the wrong copy of the build script, emits
old-format links that look perfectly correct in the code you just edited.

---

## Reference files

- `references/report-structure.md` — section-by-section content spec, table shapes, formatting
  standards, and the service-description lookup.
- `references/cost-explorer-urls.md` — the verified console URL format, the filter schema, the
  exact Cost Explorer service keys, and the URL-builder and hyperlink helpers.
- `references/example-report.docx` — a real three-month report, deidentified: every account, job,
  cluster, person, product and line-of-business word replaced by a neutral one, and the figures
  regenerated from the renamed data. Names built only from generic infrastructure words
  (`reporting-table-refresh`) come through unchanged, because they name nobody. Every number is
  the real one and they reconcile with each other.
- `${CLAUDE_PLUGIN_ROOT}/skills/glue-cost-analysis/references/service-deep-dive.md` — the
  per-resource attribution method, shared with the Glue skill rather than copied.

## Troubleshooting

- **Empty data, no services** — Cost Explorer is probably not enabled yet; it takes up to 24 hours.
- **`AccessDenied` on `ce:GetCostAndUsage`** — the role is missing it. The least-privilege policy in
  `${CLAUDE_PLUGIN_ROOT}/iam/cost-audit-policy.json` grants it.
- **Date range errors** — use the first day of each month, and an end date one day into the month
  after the last one you want.
- **Links open the Cost Explorer home screen** — the stored target is probably fine and the viewer
  rewrote it. Check the target inside the `.docx` first, then point the reader at the plain-text
  appendix.
- **Per-resource numbers do not add up to the bill** — the service API is almost certainly missing
  runs for deleted, or deleted-and-recreated, resources. See `service-deep-dive.md`.
- **Usage quantity looks absurd** (millions of "DPU-hours") — you summed every usage type,
  including the free ones. Filter to the billable usage type before aggregating.
