# Service Deep Dive — Attributing a Service's Cost to Individual Resources

Cost Explorer stops at SERVICE and USAGE_TYPE. When one service dominates a report, that is
not enough: "AWS Glue went up $1,108" is a fact, not a finding. This reference is the
procedure for pushing through to **which job, which cluster, which resource** — and for
proving the answer reconciles against the bill.

It is written around AWS Glue, where the method was developed and validated end to end
(94.4% of a $4,483 quarterly Glue bill attributed to named jobs). The last section
generalises it to other services.

> Job and account identifiers from the original engagement have been replaced with neutral
> placeholders (`<job-a>`, `<job-b>`, `<PROFILE>`, `<REGION>`), and calendar dates with their
> position in the period. Every measured number is unchanged.

---

## When to run a deep dive

Trigger on any of these:

- A single service is > 25% of period spend, or contributes > 50% of the period's net change.
- A service shows a spike the SERVICE-level narrative cannot explain ("more ETL" is not a cause).
- The user asks which job / cluster / bucket / team is responsible.
- A service appears or disappears mid-period.

Do **not** run it for a service that is flat and well understood. It is a multi-step
investigation, not a default section.

---

## The first rule: reconcile, don't estimate

**Every deep dive starts and ends with a reconciliation against billed usage.** Attribution
that does not add back up to the bill is a guess with extra steps.

Pull cost *and* usage quantity, and confirm the unit price:

```bash
aws ce get-cost-and-usage \
  --profile <PROFILE> \
  --time-period Start=2026-05-01,End=2026-08-01 \
  --granularity DAILY \
  --metrics UnblendedCost UsageQuantity \
  --filter '{"And":[{"Not":{"Dimensions":{"Key":"RECORD_TYPE","Values":["Credit","Refund"]}}},
                    {"Dimensions":{"Key":"SERVICE","Values":["AWS Glue"]}}]}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --output json > glue_daily.json
```

Then, per usage type, divide cost by quantity. A clean constant is the unit rate and
confirms you are reading the right column:

| usage type | cost | quantity | $/unit |
|---|---|---|---|
| `<REGION>-ETL-DPU-Hour` | $4,483.25 | 10,189.2 | **0.44000** |
| `<REGION>-Catalog-Request` | $0.00 | 1,491,758 | 0 |

Two things to note immediately:

- **Filter to the billable usage type before summing anything.** Free Data Catalog requests
  are ~1.5M units of noise; summing all usage types produced a nonsense "587,602 DPU-hours"
  on the first pass here.
- Daily granularity is not optional. The monthly total hides exactly the single-day spikes
  that identify the culprit, and per-day reconciliation is how you localise a gap.

Target: **≥ 90% of billed usage attributed to named resources.** Report the residual
explicitly; never quietly scale numbers up to close it.

---

## Glue: the four sources, and why one is not enough

### Source 1 — Glue `GetJobRuns` (authoritative, incomplete)

Per-run `DPUSeconds` is the billed figure. Use it whenever present.

```python
import boto3, datetime as dt
from concurrent.futures import ThreadPoolExecutor

sess = boto3.Session(profile_name="<PROFILE>", region_name="<REGION>")

def fetch(job):
    cl, out, tok = sess.client("glue"), [], None
    while True:
        kw = {"JobName": job, "MaxResults": 200}
        if tok: kw["NextToken"] = tok
        r = cl.get_job_runs(**kw)
        for run in r.get("JobRuns", []):
            out.append({
                "job": job, "id": run.get("Id"),
                "started": run["StartedOn"].astimezone(dt.timezone.utc).isoformat(),
                "exec_time": run.get("ExecutionTime") or 0,
                "max_capacity": run.get("MaxCapacity"),
                "worker_type": run.get("WorkerType"),
                "dpu_seconds": run.get("DPUSeconds"),
                "state": run.get("JobRunState"),
            })
        tok = r.get("NextToken")
        if not tok: break
    return out

def dpu_hours(r):
    if r.get("dpu_seconds"):
        return r["dpu_seconds"] / 3600.0
    mc = r.get("max_capacity") or {
        "G.025X": .25, "G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8
    }.get(r.get("worker_type"), 2)
    return mc * max(r.get("exec_time") or 0, 60) / 3600.0   # 1-minute minimum
```

> ### ⚠ The trap that invalidates this source
>
> **`GetJobRuns` only returns runs for jobs that currently exist.** Deleting a job erases
> its run history while leaving its cost on the bill.
>
> **And the dangerous case is delete-*and-recreate*.** Such a job is still in `list_jobs`,
> so it looks complete — but its history begins at the recreate date. In one account
> `<job-a>` reported 2,708 runs where CloudTrail recorded 6,551, and it was the single
> largest Glue consumer at $1,068.
>
> **Never build the run inventory by filtering CloudTrail for "job names not in
> `list_jobs`".** That is the exact filter that hides recreated jobs. Build the inventory
> from CloudTrail and use the Glue API only to enrich it.
>
> Detection: compare per-day run counts from CloudTrail against the API. There CloudTrail
> logged 1,678 starts on one day where the API returned 119.

### Source 2 — CloudTrail Event history (the true inventory, 90 days)

```python
def day_sweep(day):                       # sweep one UTC day; parallelise across days
    ct = sess.client("cloudtrail")
    a = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    out, tok = [], None
    while True:
        kw = dict(LookupAttributes=[{"AttributeKey": "EventName",
                                     "AttributeValue": "StartJobRun"}],
                  StartTime=a, EndTime=a + dt.timedelta(days=1), MaxResults=50)
        if tok: kw["NextToken"] = tok
        r = ct.lookup_events(**kw)
        for e in r["Events"]:
            ce = __import__("json").loads(e["CloudTrailEvent"])
            rp, re_ = ce.get("requestParameters") or {}, ce.get("responseElements") or {}
            if rp.get("jobName"):
                out.append([e["EventTime"].astimezone(dt.timezone.utc).isoformat(),
                            rp["jobName"], re_.get("jobRunId"), ce.get("errorCode") or ""])
        tok = r.get("NextToken")
        if not tok: break
    return out
```

- **Sweep one day per worker.** A single wide `lookup_events` call hits pagination caps and
  silently truncates — it returns newest-first and stops, so you lose the oldest days
  without any error.
- Drop events with an `errorCode` (rejected calls never ran) and those without a `jobRunId`.
- `requestParameters` carries only `jobName`; worker sizing comes from Source 4.

### Source 3 — the CloudTrail S3 trail archive (beyond 90 days)

Event history is capped at 90 days. If the account archives CloudTrail to S3, everything is
still there:

```bash
aws cloudtrail describe-trails --profile <PROFILE> --region <REGION> --output json
```

**Use S3 Select — do not download the archive.** Filtering server-side is roughly 11× faster
and moves a fraction of the bytes. Measured on 21 days of one account: local
download+decompress ran at 13 objects/s and was CPU-bound (~70 GB of JSON through Python);
S3 Select did 21,262 objects / 6.93 GB in **134 seconds**, returning 881 MB, for about
$0.02 in scanned bytes versus ~$0.63 of egress.

```python
SQL = "SELECT * FROM S3Object[*].Records[*] r WHERE r.eventSource = 'glue.amazonaws.com'"

def scan(key):
    r = s3.select_object_content(
        Bucket=BUCKET, Key=key, ExpressionType="SQL", Expression=SQL,
        InputSerialization={"JSON": {"Type": "DOCUMENT"}, "CompressionType": "GZIP"},
        OutputSerialization={"JSON": {"RecordDelimiter": "\n"}})
    buf = b""
    for ev in r["Payload"]:
        if "Records" in ev: buf += ev["Records"]["Payload"]
    return [json.loads(l) for l in buf.split(b"\n") if l.strip()]
```

Keys are `AWSLogs/{ACCOUNT}/CloudTrail/{REGION}/{YYYY}/{MM}/{DD}/`. Run ~56 workers. Filter
on `eventSource` (one cheap predicate) rather than event names — then keep
`StartJobRun`, `CreateJob`, `UpdateJob`, `DeleteJob` client-side.

If you write a progress counter, guard it with a lock — `counter += 1` across threads loses
increments and the `% 2500 == 0` check may never fire, making a working scan look hung.

### Source 4 — CloudWatch Logs stream spans (durations for lost runs)

CloudTrail gives you *that* a run happened, not how long it took. Glue's log groups
(`/aws-glue/jobs/error`, `/aws-glue/jobs/output`) typically have retention `never`, so
**streams survive job deletion**. Stream names are the `jobRunId`:

```python
def span(run_id):
    best = None
    for grp in ["/aws-glue/jobs/error", "/aws-glue/jobs/output"]:
        try:
            for st in logs.describe_log_streams(logGroupName=grp,
                                                logStreamNamePrefix=run_id,
                                                limit=20)["logStreams"]:
                if "firstEventTimestamp" in st:
                    d = (st["lastEventTimestamp"] - st["firstEventTimestamp"]) / 1000.0
                    best = d if best is None else max(best, d)
        except Exception:
            pass
    return best
```

**Validate before trusting it.** Compare spans against runs with known `DPUSeconds`:

| run length | span ÷ ExecutionTime |
|---|---|
| > 150 s | 0.94 – 0.98 ✅ |
| < 130 s | 0.44 – 0.52 ❌ — startup dominates, span misses it |

So: add a **~14 s startup offset**, apply the 1-minute floor, and treat short-run jobs as
approximate. `billed_seconds = max(span + 14, 60)`.

Sample rather than query every run — one median per (job, ISO week), ~20 runs per bucket.
Thousands of individual `describe_log_streams` calls will rate-limit.

### Worker sizing — build a **timeline**, never a single value

This is the subtlest failure mode, and it was wrong by 8× in the original engagement.

`CreateJob` / `UpdateJob` `requestParameters` carry `numberOfWorkers` + `workerType` (or
`maxCapacity`). Jobs get resized, so a single DPU per job is wrong for every run on the
other side of the change:

| job | day (UTC) | config | DPU |
|---|---|---|---|
| `<job-b>` | day 1, 17:23 | 10 × G.1X | 10 |
| | day 16, 20:19 | 10 × G.2X | 20 |
| | day 17, 20:40 | 10 × G.8X | **80** |

Taking "the last `UpdateJob` seen" priced the runs on days 17–19 at 10 DPU instead of 80.
Pricing each run at the config **in force when it ran** is what moved that month from 85% to
99.9% coverage.

```python
DPU_OF = {"G.025X": .25, "G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8}

def utc(s):
    """Normalise every timestamp to UTC before comparing. CloudTrail Event history
    isoformat() keeps a local offset (-06:00) while the S3 archive is 'Z'; comparing
    them as STRINGS silently mis-dates every resize by the offset."""
    s = s.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)

def dpu_for(job, iso, timeline, observed):
    ev = timeline.get(job)                      # [(utc_ts, dpu), ...] sorted
    if not ev:
        return statistics.mode(observed[job]) if observed.get(job) else None
    i = bisect.bisect_right([e[0] for e in ev], utc(iso)) - 1
    return ev[i][1] if i >= 0 else ev[0][1]     # extrapolate earliest config backwards
```

Precedence: per-run `MaxCapacity` from the API → the config timeline → API-observed mode.

---

## Assembling the attribution

```
for every run in the Glue API                      -> DPUSeconds (authoritative)
for every CloudTrail run NOT in the API            -> dpu_for(job, ts) × max(span+14, 60)
  (Event history for the last 90 days, S3 archive before that)
residual = billed − attributed                     -> explain it, never hide it
```

Union the inventories. Do not use CloudTrail alone as the inventory: it drops the pre-90-day
window and you will *lose* coverage you already had from the API.

---

## Validating the result — the checks that caught real bugs

**1. Coverage over 100% is proof of an error, not a rounding artifact.**
You cannot be billed for less than you attributed. June hitting 105.9% is what exposed the
timezone bug. Investigate; do not clamp.

**2. Never tune an estimate to reach 100%.**
That is curve-fitting. Fix defensible causes only. There, correcting the timezone bug landed
June at 99.9% on its own — which is *evidence*, precisely because it was not the goal. If
you find yourself choosing a DPU to make the numbers work, stop.

**3. Watch for circular inference.**
`<job-a>`'s pre-resize DPU was first inferred from the June 21–22 gap *while assuming
`<job-b>` contributed nothing on those days*. Once `<job-b>` was priced, that assumption was
false. Solve jointly: subtract everything you know, then solve for one unknown on days where
the others are absent.

```
day    gap   aRuns   bRuns  bEst   left   implied <job-a> DPU
day 3  782.4   1563       0   0.0  782.4  22.8
day 4  590.3   1176      16  72.9  517.5  18.9   -> ~20 DPU (10 × G.2X)
```

**4. Per-day residual should be flat.**
A roughly constant residual (~0.04 DPU-h per run, ~8 DPU-h/day there) is per-run
provisioning overhead that `DPUSeconds` does not capture — expected and benign. A *spiky*
residual means a missing workload. Chase the spikes; explain the baseline.

**5. Sanity-check the cleanest month.**
The month with no deletions should reconcile highest (July: 92.9%). If it doesn't, the
method is wrong, not the data.

---

## Findings this method surfaces that Cost Explorer cannot

- **Per-job cost ranking**, including jobs that no longer exist.
- **The short-run penalty.** Glue bills a 1-minute minimum. 6,541 runs at a median 65–70 s
  meant ~45% of one job's $1,068 was billing floor, not compute. The fix is batching, not
  right-sizing — a conclusion unreachable from monthly totals. (Check it; it does not always
  apply. An account with a 109 s median run pays $0.04 a quarter in floor.)
- **Failure waste.** Failed/stopped runs still bill. One job succeeded on 7 of 19 runs at
  80 DPU each; another failed all 368 of its runs.
- **Ungoverned ad-hoc work.** Manual runs with no trigger and no tag, from a named IAM user,
  recoverable only from CloudTrail.
- **Silent resizes**, up (10 → 80 DPU) and down (20 → 10 DPU, a real optimisation worth
  crediting in *Actions Taken*).
- **Legacy duplicates.** A pre-convention job still running beside its `prod_` replacement,
  often on the older, larger worker config.

---

## Reporting it

Insert as its own numbered section after *Cost Breakdown* and renumber what follows. See
`../../aws-cost-analysis/references/report-structure.md` § "Optional: Service Deep Dive".

Non-negotiables:

- **Lead with method and coverage.** State the percentage attributed and what the residual
  is before any per-job number.
- **Label every estimate.** Mark inferred figures inline with their basis and uncertainty
  ("20 DPU inferred from implied 22.8 / 18.9 on the two heaviest days; ±10%").
- **Show the DPU timeline** when a resize occurred — the reader cannot otherwise reconcile
  per-run costs against the job name.
- **Say what the data cannot show**, and what it cost to find out (the S3 scan is worth one
  sentence: 21,262 objects, 6.93 GB, 134 s).

---

## Generalising to other services

The shape holds wherever Cost Explorer stops above the resource:

| Step | Glue | EMR | Lambda | Redshift | SageMaker |
|---|---|---|---|---|---|
| Billed unit | `ETL-DPU-Hour` | `BoxUsage` | `GB-Second` | `Node-Hour` | `ML-Instance-Hour` |
| Inventory | `GetJobRuns` + CloudTrail `StartJobRun` | `ListClusters` + `RunJobFlow` | CloudWatch `Invocations` per function | `DescribeClusters` + resize events | `ListTrainingJobs` |
| Duration | log-stream spans | cluster start/end | `Duration` metric sum | node-hours | job start/end |
| Sizing over time | `CreateJob`/`UpdateJob` timeline | `ModifyInstanceGroups` | memory-size config changes | `ResizeCluster` | instance type |

Constant across all of them:

1. Reconcile against billed usage quantity at daily granularity, and confirm the unit rate.
2. Treat the service's own API as **incomplete** — deleted resources keep their cost.
3. CloudTrail is the inventory of record; CloudWatch Logs/Metrics supply durations.
4. Resource configuration is a **timeline**, not a value.
5. Normalise timestamps to UTC before any comparison.
6. Coverage > 100% means a bug. Coverage < 90% means keep digging. Never tune to fit.

**Cheapest possible win, always try first:** if the payer account allows cost-allocation
tags, activate them and none of this is necessary next quarter. In the original engagement
`ListCostAllocationTags` returned `AccessDeniedException` on the linked account — worth one
API call to find out before committing to the full reconstruction.
