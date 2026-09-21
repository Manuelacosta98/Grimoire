# Report Structure Reference

Full content specification for each section of the AWS Cost Analysis Report.

---

## Section 1: Project Overview

Simple metadata table at the top of the document. Use a clean 2-column table.

| Field | Value |
|-------|-------|
| Project Name | {project_name} |
| AWS Account ID | {account_id} |
| Created by | {author_name} — {author_role} |
| Period of Report | {start_month} – {end_month} ({n} months) |
| Report Generated | {today_date} |

---

## Section 2: Executive Summary

One or two paragraphs (not bullet points) followed by a bullet list of key findings.

**Paragraph**: Brief narrative covering total spend for the period, average monthly cost, and overall direction (increasing/decreasing/stable with % change).

**Key Findings bullet list:**
- Total spending over the period: ${total} | Average monthly: ${avg}/mo
- Overall trend: {direction} ({pct}% from first to last month)
- Largest cost reduction: {service} ({-$amount}, {-pct}%)
- Largest cost increase: {service} ({+$amount}, {+pct}%)
- Top 3 services by total cost: {svc1} (${amt}), {svc2} (${amt}), {svc3} (${amt})

**Embedded Service-Level Preview** (brief sub-section, 3–5 bullet points with the most important service changes, e.g.):
- CloudTrail eliminated in February (-$2,757.70) — Immediate impact: 24.3% monthly reduction
- AWS Glue spiked in January (+432%) then stabilized — net +$974.02 vs. start of period
- RDS on consistent downward trend (-30%) — largest absolute spend service

---

## Section 3: Service-Level Insights

**Selection is data-driven, not a fixed list.** Rank every service by its total gross spend over the period *for this specific account*, then analyze the ones that actually matter — typically the top 6–10 by spend plus any service with a large swing (e.g. >20% MoM change or a first-time appearance), even if its absolute total is small. Two different accounts will produce two completely different service lists; never force a service into the report just because it appears in the examples below, and never omit a top-spend service because it isn't listed here. The trends, root causes, and recommendations must all be derived from the numbers in front of you.

For each selected service, cover:

1. **Cost trajectory**: Month-by-month dollar amounts and % changes
2. **Trend label**: Increasing / Decreasing / Stable / Spike-then-stabilized
3. **Root cause**: Why costs moved (new workloads, cleanup, config change, growth in data, etc.)
4. **Business impact**: What this means operationally
5. **Recommendation**: The most important specific action for this service

**Format per service:**
```
N. {Service Name}: {one-line summary e.g. "Consistent downward trend (-30%)"}
   - {Month1}: ${amount} | {Month2}: ${amount} | {Month3}: ${amount}
   - Net change: {+/-$amount} ({+/-pct}%) from period start to end
   - {Root cause explanation}
   - Recommendation: {specific actionable recommendation}
```

_Reference only — common services and what tends to drive their cost. This is a memory aid for writing root causes and recommendations, **not** a checklist of services to include. Include a service because it ranks high in *this account's* spend, not because it appears here; and analyze high-spend services that are absent from this list just as thoroughly._
- Amazon RDS — often the largest absolute cost
- AWS Glue / ETL workloads
- Amazon S3 — watch for storage growth trends
- Amazon EC2 - Compute
- Amazon CloudWatch / CloudTrail
- Managed Apache Airflow (MWAA)
- Amazon QuickSight
- AWS Lambda
- Amazon VPC / Data Transfer
- Amazon Redshift / Athena
- Amazon Bedrock (Claude Opus/Sonnet/Haiku model invocations — flag a first appearance as a new AI workload to track)

---

## Section 4: Cost Breakdown

### 4.1 Current Month Costs

Table showing costs for the most recent month in the report period. Always use **gross charges** (positive amounts only) as the primary view. If credits are active, add the three footer rows described below.

| AWS Service | Gross Amount | % of Gross Total | Description |
|-------------|-------------|-----------------|-------------|
| Amazon Relational Database Service | $1,652.48 | 20.3% | Managed relational databases (Postgres / MySQL) |
| Amazon QuickSight | $951.25 | 11.7% | BI dashboards, Enterprise licenses & SPICE capacity |
| AWS Glue | $793.64 | 9.7% | Serverless ETL jobs (crawlers, DPU-hours) |
| ... | ... | ... | ... |
| **Gross Total** | **$8,144.73** | **100.0%** | Sum of all positive charges |
| Credits Applied *(if any)* | *-$X,XXX.XX* | — | Green-shaded row — itemize each credit source |
| **Net Total** *(if credits)* | **$X,XXX.XX** | — | Green-shaded row — what AWS bills after credits |

Threshold: Include all services with gross charge > $5/month. Group remaining as a single "Other Services (N items)" row with a note like "Aggregated services <$5/month each".

List **every** service above the threshold for this account, ranked by gross spend — whatever those services happen to be. The rows below are illustrative amounts from one sample account; your table reflects the real ranking and totals from the data you pulled.

**Description column** — write a short, human-readable note on what each service is actually being used for in *this* account, not a generic gloss. The lookup below covers common services only; for any service not listed, infer the description from its usage types / the account's workload. Don't restrict the table to services that appear here.

| Service | Description |
|---------|-------------|
| Amazon Relational Database Service | Managed relational databases (Postgres / MySQL) |
| Amazon QuickSight | BI dashboards, Enterprise licenses & SPICE capacity |
| AWS Glue | Serverless ETL jobs (crawlers, DPU-hours) |
| Amazon Virtual Private Cloud | NAT Gateway data processing + inter-AZ traffic |
| Amazon Managed Workflows for Apache Airflow | MWAA environment, scheduler & workers |
| AWS Support (Business) | Business-level AWS Support plan (fixed % of spend, min $100) |
| Amazon CloudWatch | Logs ingestion, log storage, custom metrics & alarms |
| Amazon Elastic Compute Cloud - Compute | EC2 instance hours |
| Amazon Simple Storage Service | Object storage — standard, IA, lifecycle costs |
| AWS Secrets Manager | Secret storage & API requests |
| AWS Config | Resource configuration recording & rule evaluations |
| EC2 - Other | EBS volumes, snapshots, NAT/Elastic IP charges |
| Amazon Elastic Container Service | ECS / Fargate task compute |
| Amazon Elastic Load Balancing | ALB / NLB load balancer hours and LCU |
| Amazon Bedrock (Claude Opus/Sonnet/Haiku) | Bedrock model invocations — AI workload |
| AWS Key Management Service | KMS keys & cryptographic operations |

#### Active Credits Notice (include when credits are detected)

Add a shaded callout paragraph above the table:

> ⚠️ **Active Credits Notice**: AWS credits are currently applied to this account. The **Gross Amount** column reflects true infrastructure cost before credits. Credits total {$amount} this month and may not renew. Plan budgets based on gross charges.

### 4.2 Historical Comparison

Side-by-side table showing all months in the period, plus MoM change column. Show **gross charges only** for service rows. If credits are active, add footer rows for Credits and Net Total.

| AWS Service | {Month1} | {Month2} | {Month3} | MoM Change ({M2}→{M3}) |
|-------------|----------|----------|----------|------------------------|
| Amazon RDS | $2,556 | $2,351 | $2,189 | -$162 (-6.9%) |
| AWS Glue | $367 | $1,953 | $1,341 | -$612 (-31.3%) |
| ... | | | | |
| **Gross Total** | **$15,947** | **$13,641** | **$11,512** | **-$2,129 (-15.6%)** |
| Credits Applied *(if any)* | *-$X,XXX* | *-$X,XXX* | *-$X,XXX* | Green-shaded |
| **Net Total** *(if credits)* | **$X,XXX** | **$X,XXX** | **$X,XXX** | Green-shaded |

Add a trend indicator: ↓ Decreasing, ↑ Increasing, → Stable

### 4.3 Credits Applied (only if credits are *material*)

> **Materiality first.** Apply the materiality gate in `SKILL.md` → *Credits: decide whether to
> mention them at all* before writing this section (or any credit content anywhere in the report). If the period credit total is below the threshold (< 1% of gross spend and < $50/month), **omit this section entirely** — along with the Active Credits Notice, the three-view footer rows, and any "gross ≈ net" footnote. Do not mention credits at all. This section, and every other credit reference in this spec, applies **only** when credits are material.

Dedicated table listing every active credit, its source, and per-month amounts:

| Credit Source | {Month1} | {Month2} | {Month3} | Period Total |
|---------------|----------|----------|----------|--------------|
| AWS Data Transfer Credit | -$8.17 | -$3.37 | -$1.55 | -$13.09 |
| Amazon QuickSight Credit | -$0.00 | -$0.00 | -$0.00 | -$0.00 |
| **Total Credits** | **-$8.17** | **-$3.37** | **-$1.55** | **-$13.09** |

Follow this table with a one-paragraph note:
> "These credits are applied automatically by AWS and reduce the billed amount. They are **not guaranteed to renew**. Infrastructure budgeting and cost planning should be based on the Gross Charges figures above."

---

## Optional: Service Deep Dive

Include this **only** when a service meets the trigger conditions in `SKILL.md` § *When a
service-level answer is not an answer*. When present it becomes its own numbered section immediately after Cost Breakdown
— renumber every following section (Cost Drivers becomes 6, Recommendations 7, Actions Taken
8, Evidence 9).

Full procedure and code:
`${CLAUDE_PLUGIN_ROOT}/skills/glue-cost-analysis/references/service-deep-dive.md`.

Structure:

- **N.1 Method and attribution coverage** — which sources were used and why, then a coverage
  table. Lead with method, not findings; a reader must know how much of the bill is
  accounted for before they read a per-job number.

  | Month | Billed | Source A | Source B | Attributed | Coverage | Residual |
  |---|---|---|---|---|---|---|
  | May 2026 | 2,073.7 | 1,456.6 | 304.1 | 1,760.7 | 84.9% | 313.1 |
  | **Period** | **10,189.2** | **6,509.9** | **3,110.1** | **9,620.0** | **94.4%** | **569.2** |

  Follow it with a "What the residual is" paragraph naming each component. Never leave an
  unexplained bucket.

- **N.2 Where the money went** — stacked-bar figure + per-resource table with a residual row.
- **N.3 The workload(s) that explain the period** — daily figure annotated with causes, then
  a sub-heading per major workload. Include a configuration timeline table if the resource
  was resized. Note whether it was scheduled or manual, tagged or untagged.
- **N.4 Per-resource ranking** — top 15 figure + top 20 table with run counts.
- **N.5 Efficiency findings** — minimum-billing floor, failure waste, sizing concentration.
- **N.6 Service-specific recommendations** — action / target / est. impact / effort table.

Rules specific to this section:

- **Label every estimate inline** with its basis and uncertainty — e.g. "20 DPU inferred
  from implied 22.8 and 18.9 on the two heaviest days; ±10%".
- **Separate confirmed from inferred.** A figure read from an API is not the same as one
  reconstructed from logs; the reader must be able to tell which is which.
- **Include a forward look** if the workload is still live, clearly marked as outside the
  report period.
- State what the investigation cost when it was non-trivial (e.g. "21,262 objects, 6.93 GB,
  134 s via S3 Select") so the reader can judge whether to repeat it.

---

## Section 5: Cost Drivers & Key Findings

### 5.1 Combined Drivers Summary

One paragraph narrative explaining the net change over the full period.

### 5.2 Cost Drivers Table

| Service | Period Change | Primary Reason |
|---------|--------------|----------------|
| AWS CloudTrail | -$2,757.70 | Service discontinued or reconfigured in February |
| AWS Glue | +$974.02 | Significant ETL job increase (spike in Jan, stabilized in Feb) |
| Amazon RDS | -$293.47 | Database optimization and rightsizing efforts |
| Amazon S3 | +$117.54 | Growing storage requirements (+43% data growth) |

These rows are illustrative — populate the table with the services that actually moved in *this* account. Include every service with absolute change > $50 over the period, regardless of which services they are.

---

## Section 6: Cost Optimization Recommendations

**Recommendations follow the data.** Write advice for the services that actually drive cost or are moving in *this* account — the same services you analyzed in Section 3 — not a fixed roster. The bullets below are a reference bank of proven tactics keyed by service; pull from it only for services present in this account, adapt the specifics to what the numbers show (e.g. don't recommend Reserved Instances for a service the account barely uses), and write fresh recommendations for high-spend services that aren't covered here. Organize by time horizon and be specific — name the service and the exact action.

### Short Term (0–30 days)

Quick wins and configuration changes (examples — apply only those relevant to this account):
- **AWS Glue**: Lock in any optimized baseline — set CloudWatch alarms on monthly DPU-hour totals; review remaining jobs for further DPU right-sizing; disable unused development endpoints
- **Amazon CloudWatch**: Apply 30-day retention to non-critical log groups; sample noisy INFO-level logs from MWAA, ECS and EKS; archive long-term retention to S3 via subscription filters
- **Amazon S3**: Audit Glue temporary output prefixes and delete stale objects; enable Intelligent-Tiering on the largest data-lake bucket
- **AWS Config**: Review recorder scope — exclude resource types that don't require compliance evidence (e.g. CodeDeploy revisions, Lambda layers); tune rule evaluation frequency
- **AWS Secrets Manager**: Audit secret inventory; migrate non-sensitive parameters to SSM Parameter Store (standard tier is free)

### Medium Term (1–3 months)

Process improvements and right-sizing:
- **Amazon RDS**: Run Cost Explorer → Rightsizing; evaluate 1-year Reserved Instances for instances running > 6 months, or Aurora Serverless v2 for variable workloads — potential 25–40% savings
- **Amazon QuickSight**: Monthly active-user audit; downgrade Enterprise users with no recent activity to Reader (~$18/user/month savings); review SPICE refresh frequency for non-critical dashboards
- **Amazon VPC**: Add VPC Gateway/Interface Endpoints for S3, DynamoDB, Secrets Manager and KMS to bypass NAT Gateway data-processing charges; audit cross-AZ chatter from EKS/ECS workloads
- **Amazon MWAA**: Right-size environment class (try `mw1.small` if currently medium); pause non-prod environments outside business hours — typical 30–50% non-prod savings
- **Amazon EC2 - Compute**: Run Compute Optimizer; evaluate Graviton (`t4g`/`m7g`/`c7g`) migration for x86 EC2 and ECS Fargate workloads — typical 20% price/performance gain

### Long Term (3+ months)

Strategic and commitment-based savings:
- **Compute Savings Plans**: 1-year, no-upfront Compute Savings Plan covering EC2 + Fargate + Lambda — up to 66% off on-demand for stable workloads
- **RDS Reserved Instances**: Buy 1-year RIs for the persistent RDS footprint
- **FinOps tagging**: Enforce mandatory cost-allocation tags (Environment, Team, Workload) on every taggable resource; turn on tag activation in Billing
- **S3 Lifecycle policies**: Transition data-lake objects to Glacier Instant Retrieval at 90 days, Deep Archive at 365 days for cold data
- **Bedrock cost governance**: Set per-team usage limits / CloudWatch alarms on Bedrock invocations before AI usage scales further
- **Architecture review**: Evaluate consolidating or serverless-ifying always-on EC2 workloads; move EMR batch jobs to Glue or EMR Serverless where feasible

---

## Section 7: Actions Taken

Document optimizations already completed during or before the report period.

Lead with an honesty note about what the data can and cannot show: only line items with a clearly visible drop in the cost data should be asserted as confirmed actions. Anything else (instance-class changes, retention-policy tweaks, license downgrades) is invisible at SERVICE-level granularity — list those as a prompt for the user to fill in rather than as fact.

| Action | Completed | Dollar Impact |
|--------|-----------|---------------|
| AWS Glue DPU / job tuning | Feb–Apr 2026 | −$547.72 / quarter (−$183/mo run-rate) |
| _<user to add: instance-class change, retention tweak, license downgrade…>_ | — | — |

Include a brief narrative summary: "Over the {period}, total monthly cost moved from ${start} to ${end} — a net {pct}% change. The biggest visible driver was {service} optimization; this was partially offset by organic growth in managed services." Then explicitly invite the user to extend the table with work performed that is not visible at SERVICE-level granularity.

---

## Section 8: Evidence

### 8.1 Cost Explorer Links

List all generated URLs (see `cost-explorer-urls.md` for construction). Present as a numbered list with label, chart type, and the full URL.

Example format:
```
1. Overall Period Analysis (STACK chart, monthly granularity)
   https://console.aws.amazon.com/cost-management/home#/cost-explorer?...

2. Amazon RDS — Daily Trend (LINE chart)
   https://console.aws.amazon.com/cost-management/home#/cost-explorer?...
```

### 8.2 Appendix: Cost Explorer URLs (plain text)

**Required in every report.** The same links as 8.1, written out in full as plain text in a
monospace run (Consolas ~6.5pt), each under a bold label.

Word and other viewers sometimes rewrite a hyperlink whose target contains a `#` fragment,
escaping the `#` and re-encoding every `%`, which sends the reader to the console home
screen instead of the filtered report. Plain text cannot be rewritten by any viewer, so this
appendix is the only delivery guaranteed to work. It costs one subsection.

Add a one-line pointer under 8.1: "If a link opens the Cost Explorer home screen instead of
the filtered report, use the plain-text URLs in 8.2 — copy the whole line into the address
bar."

### 8.3 Appendix: Complete Service Breakdown

Full table of all services with cost > $0.01 for the most recent month, sorted descending.

---

## Formatting Standards

- **Heading levels**: H1 for document title, H2 for section numbers, H3 for subsections
- **Tables**: Bordered, header row shaded light gray, currency right-aligned, totals row bold
- **Positive changes** (cost increases): Plain text or light red shading if color is available
- **Negative changes** (cost reductions): Plain text or light green shading if color is available
- **Amounts**: Always include $ sign and 2 decimal places for individual services; round to nearest dollar for totals
- **Percentages**: Always include sign (+/–) and 1 decimal place
- **Page breaks**: Insert before Section 3, Section 5, and Section 8
