# Cost Explorer URL Construction Guide

AWS Cost Explorer URLs encode all filter/display settings as query parameters. This guide explains how to build working deep-link URLs for the Evidence section.

These rules mirror the **real Cost Explorer console URL format** (the form the browser actually produces), so links open with every filter, group-by, and chart setting already applied for the signed-in user.

## Base URL (account-scoped console host)

Use the account-scoped console host that Cost Explorer itself emits:

```
https://{ACCOUNT_ID}-{HOST_HASH}.us-east-1.console.aws.amazon.com/costmanagement/home?region={REGION}#/cost-explorer
```

- `{ACCOUNT_ID}` — the 12-digit AWS account ID (from `aws sts get-caller-identity`).
- `{HOST_HASH}` — the account's console host hash (e.g. `a1b2c3d4`). This is **account-specific and not derivable** — capture it once from a URL the user copies out of their Cost Explorer address bar, then reuse it for every link in the report.
- **The host region is always `us-east-1`**, regardless of which region the user works in. Cost Explorer is a global/us-east-1 console service. Do **not** put the data region in the hostname — `…{HASH}.us-west-2.console.aws.amazon.com` is wrong and will not resolve to a working report.
- `{REGION}` — the user's data region (e.g. `us-west-2`). It travels as a **`?region=` query parameter placed BEFORE the `#` fragment**, not in the hostname.
- The path is `costmanagement/home` (no hyphen) — not `cost-management/home`.

A correct, complete example (extracted verbatim from a shipped report):

```
https://123456789012-a1b2c3d4.us-east-1.console.aws.amazon.com/costmanagement/home?region=us-west-2#/cost-explorer?chartStyle=GROUP&costAggregate=unBlendedCost&startDate=2026-04-01&endDate=2026-06-30&granularity=Monthly&groupBy=%5B%22Service%22%5D&filter=%5B%5D&excludeForecasting=false&futureRelativeRange=CUSTOM&historicalRelativeRange=CUSTOM&isDefault=true&reportMode=STANDARD&reportName=New+cost+and+usage+report&showOnlyUncategorized=false&showOnlyUntagged=false&usageAggregate=undefined&useNormalizedUnits=false
```

Note the two `?` — one for the pre-fragment `region` param, one opening the fragment's own query string. The path is `costmanagement/home` (no hyphen).

**Match this parameter list, order and encoding exactly.** It is lifted from a shipped report whose links are confirmed working, not reconstructed from a browser paste:

1. `chartStyle` 2. `costAggregate` 3. `startDate` 4. `endDate` 5. `granularity` 6. `groupBy` 7. `filter` 8. `excludeForecasting` 9. `futureRelativeRange` 10. `historicalRelativeRange` 11. `isDefault` 12. `reportMode` 13. `reportName` 14. `showOnlyUncategorized` 15. `showOnlyUntagged` 16. `usageAggregate` 17. `useNormalizedUnits`

- The order is **not alphabetical** — keep it as listed.
- `isDefault=true` **is** part of the format. Do not drop it.
- Spaces encode as `+`, not `%20` — i.e. plain `urlencode` (`quote_plus`). Do **not** pass `quote_via=quote`.
- `historicalRelativeRange=CUSTOM` is correct for an explicit date range.

**Before inventing a format, read one out of a shipped report.** `references/example-report.docx` carries eight links in the verified shape — extract one and copy it:

```python
import zipfile, re
z = zipfile.ZipFile("references/example-report.docx")
rels = z.read("word/_rels/document.xml.rels").decode()
print([t.replace("&amp;", "&") for t in
       re.findall(r'<Relationship[^>]*hyperlink[^>]*Target="([^"]+)"', rels)][0])
```

Then generate a URL with that report's own dates and assert byte-equality with what you extracted. A URL a user pastes from their address bar has often been rewritten by whatever opened it, so it is weaker evidence than a shipped artifact.

**Storage in the .docx, and the fallback that always works.** Put the full URL — fragment included — in the hyperlink relationship `Target`, exactly as the shipped reports do. Do **not** split it and put the fragment in `w:anchor`; that was tried and is not what the working artifacts use. Some viewers rewrite a `Target` containing `#` on open (escaping `#` to `%23` and every `%` to `%25`, which collapses the fragment into the `?region=` value and lands the reader on `#/home`). That is a viewer-side rewrite, not a defect in the stored target — so verify the stored target first rather than changing the storage.

Because that rewrite is outside your control, **every report must also carry a plain-text appendix listing each Cost Explorer URL verbatim in a monospace run** (see `report-structure.md` § Evidence). Plain text cannot be rewritten by any viewer, costs one extra subsection, and is the only delivery guaranteed to work.

**Verify links by reading them back out of the finished `.docx`**, never by inspecting the generating script:

```python
import re, zipfile
z = zipfile.ZipFile(out_path)
rels = z.read("word/_rels/document.xml.rels").decode()
for t in re.findall(r'<Relationship[^>]*hyperlink[^>]*Target="([^"]+)"', rels):
    u = t.replace("&amp;", "&")
    assert u.startswith(f"https://{ACCOUNT_ID}-{HOST_HASH}.us-east-1.console.aws.amazon.com"
                        f"/costmanagement/home?region={REGION}#/cost-explorer?"), u
```

A stale duplicate helper definition, or a patch applied to the wrong copy of the build script, silently emits old-format links that look correct in the code you just edited.

> **If the host hash is unknown**: ask the user to paste one Cost Explorer URL from their browser and lift the `{ACCOUNT_ID}-{HOST_HASH}` host from it. Do not fall back to a generic host — these links are meant to open directly in the user's console.

---

## Period dates — ALWAYS match the report period

This is the one rule that does **not** come from a copied browser URL. Browser URLs default to a rolling "last N days" window; a report must not.

- **`startDate`** = first day of the first month in the report period (e.g. `2026-03-01`).
- **`endDate`** = last day of the last month in the report period, inclusive (e.g. `2026-05-31`).
- This applies to **every** link — the monthly overview **and** the daily per-service trends. A daily trend link still spans the full period (`2026-03-01 → 2026-05-31`), never a rolling `last-30-days` window and never a date that bleeds past the period end.
- For a single-month report, use that month's first and last day.

Never emit `startDate`/`endDate` values outside the report period.

---

## Core Parameters

The full parameter set, in the **fixed order listed above** (not alphabetical), after the `#/cost-explorer?` fragment. Include all seventeen for a faithful, ready-to-read link.

| Parameter | Purpose | Common Values |
|-----------|---------|---------------|
| `chartStyle` | Visualization type | `GROUP` (overall), `STACK` (multi-service), `LINE` (single/few-service trend), `BAR` |
| `costAggregate` | Cost type | `unBlendedCost` (standard), `amortizedCost` (for RIs/SPs) |
| `startDate` | First day of period | `2026-03-01` — **see period rule above** |
| `endDate` | Last day of period (inclusive) | `2026-05-31` — **see period rule above** |
| `granularity` | Data resolution | `Monthly`, `Daily` |
| `groupBy` | Dimension(s) to break down by — JSON array | `["Service"]`, `["UsageType"]`, `["LinkedAccount"]` |
| `filter` | Service/tag filters — URL-encoded JSON (see schema below) | `[]` for no filter |
| `excludeForecasting` | Hide projected data | `false` (monthly overview), `true` (daily trend) |
| `futureRelativeRange` | Forecast range mode | `CUSTOM` |
| `historicalRelativeRange` | History range mode | `CUSTOM` for an explicit date range; the console emits `LAST_6_MONTHS` etc. when a preset picker was used |
| `isDefault` | Default report flag | `true` — part of the working format, do not omit |
| `reportMode` | Report mode | `STANDARD` |
| `reportName` | Report label | `New cost and usage report` |
| `showOnlyUncategorized` | Show only uncategorized | `false` |
| `showOnlyUntagged` | Show only untagged | `false` |
| `usageAggregate` | Usage metric | `undefined` |
| `useNormalizedUnits` | Normalize instance types | `false` |

---

## Filter Schema (console format)

The `filter` parameter is a URL-encoded JSON array. Each clause names a dimension object and a list of `{value, displayValue}` pairs — `value` is the exact Cost Explorer API service key, `displayValue` is the friendly console label.

Decoded, single service (RDS):
```json
[
  {
    "dimension": { "id": "Service", "displayValue": "Service" },
    "operator": "INCLUDES",
    "values": [
      { "value": "Amazon Relational Database Service", "displayValue": "Relational Database Service (RDS)" }
    ]
  }
]
```

Multiple services go in the same `values` array. Use `"[]"` (encoded `%5B%5D`) for the unfiltered overview.

> **`value` vs `displayValue`**: `value` must be the **exact API key** returned by `aws ce get-cost-and-usage` group-by SERVICE (e.g. `Amazon Relational Database Service`, `AmazonCloudWatch`, `AWS Glue`) — *not* the friendly label. `displayValue` is cosmetic. Mismatching `value` produces an empty chart.

---

## Standard Report URLs

### 1. Overall Period — GROUP Chart (All Services)

`chartStyle=GROUP`, `granularity=Monthly`, `groupBy=["Service"]`, `filter=[]`, dates = full period.

### 2. Top / Selected Services — STACK Chart

`chartStyle=STACK`, `granularity=Monthly`, `groupBy=["Service"]`, `filter` = INCLUDES clause listing the chosen services, dates = full period.

### 3. Single Service — LINE Chart (daily trend, drilled by usage type)

`chartStyle=LINE`, `granularity=Daily`, `groupBy=["UsageType"]`, `excludeForecasting=true`, `filter` = INCLUDES clause for the one service, dates = **full report period** (not a rolling window).

---

## Service Name Exact Strings

Use the exact API key in `value` (left column); the `displayValue` (right column) is the friendly label. AWS is case-sensitive.

| `value` (API key — use in filter) | `displayValue` (friendly label) |
|-----------------------------------|---------------------------------|
| `Amazon Relational Database Service` | `Relational Database Service (RDS)` |
| `Amazon Simple Storage Service` | `S3 (Simple Storage Service)` |
| `Amazon Elastic Compute Cloud - Compute` | `EC2-Instances (Elastic Compute Cloud - Compute)` |
| `AWS Glue` | `Glue` |
| `AmazonCloudWatch` | `CloudWatch` |
| `AWS CloudTrail` | `CloudTrail` |
| `Amazon Managed Workflows for Apache Airflow` | `Managed Workflows for Apache Airflow` |
| `Amazon QuickSight` | `QuickSight` |
| `Amazon Virtual Private Cloud` | `VPC (Virtual Private Cloud)` |
| `AWS Lambda` | `Lambda` |
| `Amazon Athena` | `Athena` |
| `Amazon Redshift` | `Redshift` |
| `AWS Secrets Manager` | `Secrets Manager` |
| `AWS Config` | `Config` |
| `AWS Support (Business)` | `Support (Business)` |
| `Amazon Elastic Container Service` | `Elastic Container Service (ECS)` |
| `Amazon Elastic Load Balancing` | `Elastic Load Balancing` |
| `AWS Key Management Service` | `Key Management Service (KMS)` |
| `Amazon Elastic MapReduce` | `EMR (Elastic MapReduce)` |
| `Claude Opus 4.6 (Amazon Bedrock Edition)` | `Claude Opus 4.6 (Amazon Bedrock Edition)` |
| `Claude Sonnet 4.5 (Amazon Bedrock Edition)` | `Claude Sonnet 4.5 (Amazon Bedrock Edition)` |
| `Claude Haiku 4.5 (Amazon Bedrock Edition)` | `Claude Haiku 4.5 (Amazon Bedrock Edition)` |

---

## URL Encoding Reference

For building filter JSON manually:

| Character | Encoded |
|-----------|---------|
| `[` | `%5B` |
| `]` | `%5D` |
| `{` | `%7B` |
| `}` | `%7D` |
| `"` | `%22` |
| `:` | `%3A` |
| `,` | `%2C` |
| ` ` (in a query value) | `+` — `urlencode`/`quote_plus` default; this is what the console emits |

---

## Recommended URL Set per Report

Include these links in every report's Evidence section. Each link must be rendered as a **clickable hyperlink** with a short display title — never as a raw URL. Use the `add_hyperlink` helper below.

| # | Short Title (display text) | Chart type |
|---|---------------------------|------------|
| 1 | Overall Period — All Services | GROUP monthly |
| 2 | Top Services Comparison | STACK monthly |
| 3 | {#1 cost service} — Trend | LINE daily |
| 4 | {Largest increase service} — Trend | LINE daily |
| 5 | {Largest reduction service} — Trend | LINE daily |
| 6 | S3 Storage Growth | LINE daily |

Add additional service-specific links for any service with >20% MoM change or >$500 absolute change. **Always flag a new AI/Bedrock workload with its own trend link** — a first-appearing service is exactly the "largest increase" case link #4 is meant to capture.

**Rendering rule**: In the Evidence section, present each link as:
```
[bold short title]  →  [clickable hyperlink with short title as display text]
```
Never print the raw URL inline in the document body.

---

## Python Helpers: URL Builder + Hyperlink Renderer

```python
import urllib.parse, json
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def cost_explorer_url(account_id, host_hash, region, start, end,
                      chart="GROUP", granularity="Monthly",
                      group_by=None, services=None, exclude_forecasting=None):
    """Build a Cost Explorer deep-link URL matching the verified template.

    Host region is ALWAYS us-east-1; the data region travels as ?region= before
    the '#'. Params keep the fixed (non-alphabetical) order, include
    isDefault=true, and encode spaces as '+' via plain urlencode.

    start / end MUST be the report period bounds -- never a rolling window.
    """
    base = (f"https://{account_id}-{host_hash}.us-east-1.console.aws.amazon.com"
            f"/costmanagement/home?region={region}#/cost-explorer")
    if group_by is None:
        group_by = ["Service"]
    if exclude_forecasting is None:
        exclude_forecasting = (granularity == "Daily")

    if services:
        filt = [{
            "dimension": {"id": "Service", "displayValue": "Service"},
            "operator": "INCLUDES",
            "values": [{"value": s, "displayValue": s} if isinstance(s, str)
                       else {"value": s[0], "displayValue": s[1]} for s in services],
        }]
        filter_val = json.dumps(filt, separators=(",", ":"))
    else:
        filter_val = "[]"

    params = [
        ("chartStyle", chart),
        ("costAggregate", "unBlendedCost"),
        ("startDate", start),
        ("endDate", end),
        ("granularity", granularity),
        ("groupBy", json.dumps(group_by, separators=(",", ":"))),
        ("filter", filter_val),
        ("excludeForecasting", "true" if exclude_forecasting else "false"),
        ("futureRelativeRange", "CUSTOM"),
        ("historicalRelativeRange", "CUSTOM"),
        ("isDefault", "true"),
        ("reportMode", "STANDARD"),
        ("reportName", "New cost and usage report"),
        ("showOnlyUncategorized", "false"),
        ("showOnlyUntagged", "false"),
        ("usageAggregate", "undefined"),
        ("useNormalizedUnits", "false"),
    ]
    return base + "?" + urllib.parse.urlencode(params)   # quote_plus: spaces -> '+'


def add_hyperlink(paragraph, text, url):
    """
    Add a clickable hyperlink to a python-docx paragraph.
    Renders as blue underlined text — never shows the raw URL.

    The full URL, fragment included, goes in the relationship Target -- this
    matches the shipped reports whose links open correctly.

    Usage:
        p = doc.add_paragraph()
        p.add_run("3. RDS Daily Trend  -> ").bold = True
        add_hyperlink(p, "Open in Cost Explorer", url)
    """
    part = paragraph.part
    r_id = part.relate_to(url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    # Blue + underline styling
    color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1"); rPr.append(color)
    u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
    new_run.append(rPr)
    t = OxmlElement("w:t"); t.text = text; new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink
```

Usage in Evidence section (note the period dates are reused on every link):

```python
ACCOUNT_ID, HOST_HASH, REGION = "123456789012", "a1b2c3d4", "us-west-2"   # REGION = data region
START, END = "2026-03-01", "2026-05-31"   # report period — same on every link

links = [
    ("Overall Period — All Services",
        cost_explorer_url(ACCOUNT_ID, HOST_HASH, REGION, START, END, chart="GROUP")),
    ("Top Services Comparison",
        cost_explorer_url(ACCOUNT_ID, HOST_HASH, REGION, START, END, chart="STACK",
                          services=["Amazon Relational Database Service", "AWS Glue"])),
    ("Amazon RDS — Daily Trend",
        cost_explorer_url(ACCOUNT_ID, HOST_HASH, REGION, START, END, chart="LINE",
                          granularity="Daily", group_by=["UsageType"],
                          services=["Amazon Relational Database Service"])),
]
for i, (title, url) in enumerate(links, 1):
    p = doc.add_paragraph()
    p.add_run(f"{i}. {title}  →  ").bold = True
    add_hyperlink(p, "Open in Cost Explorer", url)
```
