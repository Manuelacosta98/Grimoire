#!/usr/bin/env python3
"""Attribute AWS Glue spend to individual jobs and render a self-contained HTML dashboard.

Cost Explorer stops at SERVICE and USAGE_TYPE. This script pushes through to the job:
it reconciles billed DPU-hours against every run returned by GetJobRuns, reports the
coverage it achieved, and never tunes an estimate to close the gap.

Standard library only. Shells out to the `aws` CLI, so it uses whatever profile and
credentials the caller already has.

Usage:
    python3 glue_cost_report.py --profile <AWS_PROFILE> [--region us-east-1]
                                [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--out PATH]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DPU_OF = {"G.025X": 0.25, "G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8,
          "Standard": 1, "Z.2X": 2}
STATES = ["SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "RUNNING", "WAITING", "ERROR",
          "ESTIMATED"]   # reconstructed from CloudTrail: it billed, but its outcome is unknown
WORKERS = ["G.1X", "G.2X", "G.4X", "G.8X", "G.025X", "Standard", "Z.2X"]
BAD_STATES = {"FAILED", "TIMEOUT", "STOPPED", "ERROR"}
BILLABLE_STATES = {"SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "ESTIMATED"}
SPAN_STARTUP = 14       # log spans miss container startup; validated against known DPUSeconds
LOOKBACK_DAYS = 90      # default window, and the reach of CloudTrail Event history
STALE_DAYS = 90         # a job idle longer than this reads as stale in the inventory
# Naming conventions are per-organisation. These are only a common default: an
# account that does not use them gets no "outside the convention" finding at all,
# because the finding is gated on at least a fifth of jobs actually matching one.
DEFAULT_ENV_PREFIXES = ("prod_", "dev_", "sandbox_")
ENV_PREFIXES: tuple[str, ...] = DEFAULT_ENV_PREFIXES
ENV_NAMES: tuple[str, ...] = tuple(p.rstrip("_-.") for p in DEFAULT_ENV_PREFIXES)
ENV_PREFIX_BY_NAME: dict[str, str] = {p.rstrip("_-."): p for p in DEFAULT_ENV_PREFIXES}
SAMPLE_RUNS = 20        # runs averaged per job, and sampled per bucket for log spans
HEAVY_RUNS = 40         # reconstructed runs above which a job is sampled daily, not weekly
MAX_PAGES = 40          # 8,000 runs per job is deeper than Glue's retention ever goes
FLAG_KEYS = ("--enable-metrics", "--enable-observability-metrics", "--enable-spark-ui",
             "--enable-auto-scaling", "--job-bookmark-option", "--datalake-formats",
             "--enable-continuous-cloudwatch-log", "--enable-glue-datacatalog", "--TempDir")

# Every IAM action this script calls, and nothing it does not. CI diffs this list
# against iam/cost-audit-policy.json and iam/cost-audit-role.yaml, so the role can
# never drift from what the code needs. All of them are reads.
REQUIRED_ACTIONS = [
    "ce:GetCostAndUsage",            # billed DPU-hours and the unit rate
    "cloudtrail:LookupEvents",       # --reconstruct: runs of jobs that no longer exist
    "glue:GetJobRuns",               # per-run DPUSeconds, the figure AWS bills
    "glue:GetJobs",                  # job inventory and configuration
    "glue:GetTrigger",               # what starts each job
    "glue:ListTriggers",
    "logs:DescribeLogStreams",       # --reconstruct: durations the Glue API lost
    "s3:GetObject",                  # --archive-bucket: S3 Select over the trail archive
    "s3:ListBucket",                 # --archive-bucket: finding the day partitions
    "sts:GetCallerIdentity",
]


# --------------------------------------------------------------------------- aws

class AwsError(RuntimeError):
    pass


def aws(profile: str, region: str, *args: str, allow_fail: bool = False) -> dict | None:
    """Run one aws CLI call and parse its JSON. Returns None when allow_fail swallows it."""
    cmd = ["aws", *args, "--profile", profile, "--region", region, "--output", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if allow_fail:
            return None
        raise AwsError(f"{' '.join(args[:2])} failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout or "{}")


def account_id(profile: str, region: str) -> str:
    out = aws(profile, region, "sts", "get-caller-identity", allow_fail=True)
    return (out or {}).get("Account", "unknown")


# ----------------------------------------------------------------------- fetching

def fetch_cost(profile: str, region: str, start: str, end: str) -> dict:
    """Daily Glue cost and usage by usage type, credits and refunds excluded."""
    flt = json.dumps({"And": [
        {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}},
        {"Dimensions": {"Key": "SERVICE", "Values": ["AWS Glue"]}}]})
    return aws(profile, region, "ce", "get-cost-and-usage",
               "--time-period", f"Start={start},End={end}",
               "--granularity", "DAILY",
               "--metrics", "UnblendedCost", "UsageQuantity",
               "--filter", flt,
               "--group-by", "Type=DIMENSION,Key=USAGE_TYPE")


def fetch_jobs(profile: str, region: str) -> list[dict]:
    jobs, token = [], None
    while True:
        args = ["glue", "get-jobs", "--max-results", "200"]
        if token:
            args += ["--next-token", token]
        page = aws(profile, region, *args)
        jobs.extend(page.get("Jobs", []))
        token = page.get("NextToken")
        if not token:
            break
    return jobs


def fetch_runs(profile: str, region: str, names: list[str]) -> list[dict]:
    """Every run Glue still retains, for every job, in parallel."""
    def one(job: str) -> list[dict]:
        out, token, pages = [], None, 0
        while pages < MAX_PAGES:
            args = ["glue", "get-job-runs", "--job-name", job, "--max-results", "200"]
            if token:
                args += ["--next-token", token]
            page = aws(profile, region, *args, allow_fail=True)
            if page is None:
                break
            pages += 1
            for run in page.get("JobRuns", []):
                if not run.get("StartedOn"):
                    continue
                out.append({"job": job, "id": run.get("Id"), "started": run["StartedOn"],
                            "exec": run.get("ExecutionTime") or 0,
                            "dpu_seconds": run.get("DPUSeconds"),
                            "worker": run.get("WorkerType"),
                            "workers": run.get("NumberOfWorkers"),
                            "max_capacity": run.get("MaxCapacity"),
                            "state": run.get("JobRunState"),
                            "trigger": run.get("TriggerName"),
                            "error": (run.get("ErrorMessage") or "")[:400]})
            token = page.get("NextToken")
            if not token:
                break
        return out

    with ThreadPoolExecutor(max_workers=16) as pool:
        return [r for chunk in pool.map(one, names) for r in chunk]


def fetch_triggers(profile: str, region: str) -> dict[str, list[dict]]:
    """job name -> the Glue triggers that start it. Skipped silently without permission."""
    listing = aws(profile, region, "glue", "list-triggers", "--max-results", "200",
                  allow_fail=True)
    if not listing:
        return {}
    by_job: dict[str, list[dict]] = collections.defaultdict(list)

    def one(name: str) -> None:
        got = aws(profile, region, "glue", "get-trigger", "--name", name, allow_fail=True)
        if not got:
            return
        trg = got.get("Trigger", {})
        for action in trg.get("Actions", []):
            if action.get("JobName"):
                by_job[action["JobName"]].append(
                    {"n": name, "ty": trg.get("Type"), "sch": trg.get("Schedule", ""),
                     "st": trg.get("State"), "wf": trg.get("WorkflowName", "")})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(one, listing.get("TriggerNames", [])))
    return dict(by_job)


# ---------------------------------------------------------------------- computing

def parse_ts(value: str) -> dt.datetime:
    """Normalise every timestamp to UTC before comparing anything."""
    text = value.replace("Z", "+00:00")
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def run_dpu(run: dict) -> float:
    if run.get("worker"):
        return DPU_OF.get(run["worker"], 2) * (run.get("workers") or 0)
    return run.get("max_capacity") or 2


def run_hours(run: dict) -> float:
    """DPUSeconds is what AWS bills. Fall back to the run's own worker config only when absent."""
    if run.get("dpu_seconds"):
        return run["dpu_seconds"] / 3600.0
    return run_dpu(run) * max(run.get("exec") or 0, 60) / 3600.0   # 1-minute floor


def billable_usage_type(totals: dict[str, tuple[float, float]]) -> str | None:
    """The region-prefixed ETL DPU-hour line — never the free Data Catalog requests."""
    etl = [k for k in totals if k.endswith("ETL-DPU-Hour")]
    return max(etl, key=lambda k: totals[k][0]) if etl else None


def compute(cost: dict, jobs: list[dict], runs: list[dict],
            triggers: dict, start: str, end: str) -> dict:
    """Reconcile, attribute, and shape everything the page needs."""
    # ---- the bill, per usage type and per day
    totals: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0])
    per_day: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for bucket in cost.get("ResultsByTime", []):
        day = bucket["TimePeriod"]["Start"]
        for group in bucket.get("Groups", []):
            key = group["Keys"][0]
            usd = float(group["Metrics"]["UnblendedCost"]["Amount"])
            qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
            totals[key][0] += usd
            totals[key][1] += qty
            per_day[key][day] = per_day[key].get(day, 0.0) + qty
    frozen = {k: (v[0], v[1]) for k, v in totals.items()}
    etl = billable_usage_type(frozen)
    if not etl:
        raise AwsError("No *-ETL-DPU-Hour usage found for AWS Glue in this period. "
                       "Either the account runs no Glue ETL jobs, or Cost Explorer has "
                       "not finished populating the window.")
    bill_usd, bill_qty = frozen[etl]
    rate = round(bill_usd / bill_qty, 5) if bill_qty else 0.44
    bill_day = per_day[etl]

    crawler = sum(v[0] for k, v in frozen.items() if "Crawler-DPU-Hour" in k)
    crawler_h = sum(v[1] for k, v in frozen.items() if "Crawler-DPU-Hour" in k)
    session = sum(v[0] for k, v in frozen.items() if "InteractiveSession" in k)

    # ---- attribution, restricted to the billed window
    window = [r for r in runs
              if start <= r["started"][:10] < end
              and r.get("state") in BILLABLE_STATES]
    by_day_src: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: collections.defaultdict(float))
    by_day: dict[str, float] = collections.defaultdict(float)
    by_job: dict[str, float] = collections.defaultdict(float)
    by_month: dict[str, float] = collections.defaultdict(float)
    job_month: dict[tuple[str, str], float] = collections.defaultdict(float)
    job_runs: collections.Counter = collections.Counter()
    for run in window:
        hours = run_hours(run)
        day = run["started"][:10]
        by_day[day] += hours
        by_day_src[day][run.get("src", "api")] += hours
        by_job[run["job"]] += hours
        by_month[day[:7]] += hours
        job_month[(run["job"], day[:7])] += hours
        job_runs[run["job"]] += 1
    attributed = sum(by_job.values())

    bill_month: dict[str, float] = collections.defaultdict(float)
    for day, qty in bill_day.items():
        bill_month[day[:7]] += qty

    # ---- per-day coverage, and the days that do not behave
    covs = [by_day.get(d, 0.0) / q for d, q in bill_day.items() if q > 1]
    anomalies = sorted(((d, q, by_day.get(d, 0.0)) for d, q in bill_day.items()
                        if q > 1 and not 0.85 <= by_day.get(d, 0.0) / q <= 1.15),
                       key=lambda t: -abs(t[2] - t[1]))[:2]

    # ---- charts
    months = sorted(bill_month)
    ranked = sorted(by_job.items(), key=lambda kv: -kv[1])
    top15 = [j for j, _ in ranked[:15]]
    top6 = top15[:6]
    comp = []
    for month in months:
        row = {"m": month, "bill": round(bill_month[month], 2)}
        for job in top6:
            row[job] = round(job_month.get((job, month), 0.0), 2)
        row["__other__"] = round(
            sum(v for (j, m), v in job_month.items() if m == month and j not in top6), 2)
        row["__resid__"] = round(max(bill_month[month] - by_month.get(month, 0.0), 0.0), 2)
        comp.append(row)

    bands = [("<1 min", 0, 60), ("1-2 min", 60, 120), ("2-5 min", 120, 300),
             ("5-15 min", 300, 900), ("15-60 min", 900, 3600), (">1 h", 3600, 10 ** 9)]
    duration = []
    for label, low, high in bands:
        subset = [r for r in window if low <= (r.get("exec") or 0) < high]
        duration.append({"label": label, "runs": len(subset),
                         "dpuh": round(sum(run_hours(r) for r in subset), 2)})

    return {
        "rate": rate, "etl": etl, "bill_usd": bill_usd, "bill_qty": bill_qty,
        "bill_day": bill_day, "bill_month": dict(bill_month),
        "by_day": dict(by_day), "by_job": dict(by_job), "by_month": dict(by_month),
        "by_day_src": {d: dict(v) for d, v in by_day_src.items()},
        "job_month": job_month, "job_runs": job_runs, "attributed": attributed,
        "covs": covs, "anomalies": anomalies, "months": months,
        "top15": top15, "top6": top6, "comp": comp, "duration": duration,
        "window": window, "crawler": crawler, "crawler_h": crawler_h, "session": session,
    }


def inventory_rows(jobs: list[dict], runs: list[dict], rate: float, now: dt.datetime) -> list[dict]:
    """One row per job: version, worker, last run, and DPU-hours averaged over its last 20 runs."""
    by_job: dict[str, list[dict]] = collections.defaultdict(list)
    for run in runs:
        by_job[run["job"]].append(run)
    rows = []
    for job in jobs:
        name = job["Name"]
        history = sorted(by_job.get(name, []), key=lambda r: r["started"], reverse=True)
        billable = [r for r in history
                    if r.get("state") in BILLABLE_STATES and (r.get("exec") or 0) > 0][:SAMPLE_RUNS]
        hours = [run_hours(r) for r in billable]
        secs = [r["exec"] for r in billable]
        args = job.get("DefaultArguments") or {}
        command = job.get("Command") or {}
        last = history[0] if history else None
        last_ts = parse_ts(last["started"]) if last else None
        rows.append({
            "job_name": name,
            "glue_version": job.get("GlueVersion", ""),
            "worker": (f"{job['WorkerType']} x{job.get('NumberOfWorkers')}"
                       if job.get("WorkerType") else command.get("Name", "")),
            "last_run": last_ts.strftime("%Y-%m-%d %H:%M") if last_ts else "NUNCA",
            "last_state": last.get("state", "") if last else "-",
            "days_ago": (now - last_ts).days if last_ts else None,
            "runs_sampled": len(hours),
            "avg_dpu_hours": round(statistics.mean(hours), 3) if hours else None,
            "avg_exec_min": round(statistics.mean(secs) / 60, 1) if secs else None,
            "avg_cost_run_usd": round(statistics.mean(hours) * rate, 3) if hours else None,
            "enable_metrics": args.get("--enable-metrics", "-"),
            "enable_obs_metrics": args.get("--enable-observability-metrics", "-"),
            "enable_spark_ui": args.get("--enable-spark-ui", "-"),
            "created_on": job["CreatedOn"][:10],
            "last_modified": job["LastModifiedOn"][:10],
        })
    rows.sort(key=lambda r: r["job_name"].lower())
    return rows


def detail_blob(jobs: list[dict], runs: list[dict], triggers: dict) -> dict:
    """Compact per-job run history. Strings are deduplicated into side tables."""
    base = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    trig_names = sorted({r["trigger"] for r in runs if r.get("trigger")})
    errors = sorted({r["error"] for r in runs if r.get("error")})
    t_index = {t: i for i, t in enumerate(trig_names)}
    e_index = {e: i for i, e in enumerate(errors)}
    s_index = {s: i for i, s in enumerate(STATES)}
    w_index = {w: i for i, w in enumerate(WORKERS)}

    by_job: dict[str, list[list]] = collections.defaultdict(list)
    for run in sorted(runs, key=lambda r: r["started"]):
        stamp = parse_ts(run["started"])
        by_job[run["job"]].append([
            int((stamp - base).total_seconds() // 60),
            run.get("exec") or 0,
            int(round(run["dpu_seconds"])) if run.get("dpu_seconds") else -1,
            s_index.get(run.get("state"), -1),
            w_index.get(run.get("worker"), -1),
            run.get("workers") or 0,
            t_index.get(run.get("trigger"), -1),
            e_index.get(run.get("error"), -1),
        ])

    meta = {}
    for job in jobs:
        args = job.get("DefaultArguments") or {}
        command = job.get("Command") or {}
        meta[job["Name"]] = {
            "gv": job.get("GlueVersion", ""), "wt": job.get("WorkerType", ""),
            "nw": job.get("NumberOfWorkers", 0), "cmd": command.get("Name", ""),
            "py": command.get("PythonVersion", ""), "script": command.get("ScriptLocation", ""),
            "role": (job.get("Role") or "").split("/")[-1],
            "to": job.get("Timeout"), "retry": job.get("MaxRetries", 0),
            "conc": (job.get("ExecutionProperty") or {}).get("MaxConcurrentRuns", 1),
            "conn": (job.get("Connections") or {}).get("Connections", []),
            "created": job["CreatedOn"][:10], "modified": job["LastModifiedOn"][:10],
            "desc": (job.get("Description") or "")[:200],
            "flags": {k: str(v) for k, v in args.items() if k in FLAG_KEYS},
            "nargs": len(args),
        }
    return {"base": base.isoformat(), "states": STATES, "workers": WORKERS,
            "trigs": trig_names, "errs": errors, "runs": dict(by_job),
            "meta": meta, "trigmap": triggers}


# ------------------------------------------------- reconstruction (sources 2, 3 and 4)
#
# The Glue API only retains runs for jobs that still exist. Deleting a job — or deleting
# and recreating it — erases its history while leaving its cost on the bill. Everything
# below recovers those runs. It is opt-in because it sweeps CloudTrail and CloudWatch Logs.

def sweep_cloudtrail(profile: str, region: str, event: str,
                     start: dt.date, end: dt.date, say) -> tuple[list[dict], list[str]]:
    """One UTC day per task. Never pass --max-results: the CLI then returns its own
    pagination token, --next-token rejects it, and every day silently caps at one page."""
    def fetch(a: str, b: str, tries: int = 6) -> list[dict] | None:
        cmd = ["aws", "cloudtrail", "lookup-events",
               "--lookup-attributes", f"AttributeKey=EventName,AttributeValue={event}",
               "--start-time", a, "--end-time", b,
               "--profile", profile, "--region", region, "--output", "json"]
        delay = 2.0
        for _ in range(tries):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                try:
                    page = json.loads(proc.stdout or "{}")
                except json.JSONDecodeError:
                    return None
                out = []
                for e in page.get("Events", []):
                    rec = json.loads(e["CloudTrailEvent"])
                    if rec.get("errorCode"):          # rejected calls never ran
                        continue
                    rp = rec.get("requestParameters") or {}
                    re_ = rec.get("responseElements") or {}
                    out.append({"t": e["EventTime"], "ev": event,
                                "job": rp.get("jobName") or rp.get("name"),
                                "id": re_.get("jobRunId"), "rp": rp})
                return out
            time.sleep(delay)
            delay *= 1.8                              # lookup-events is ~2 req/s per account
        return None

    def one(day: dt.date) -> tuple[list[dict], str | None]:
        width = 1 if event == "StartJobRun" else 7
        stop = min(day + dt.timedelta(days=width), end)
        got = fetch(f"{day.isoformat()}T00:00:00Z", f"{stop.isoformat()}T00:00:00Z")
        if got is not None:
            return got, None
        # A day busy enough to exhaust the retries is exactly the day that matters. Split it
        # into quarters and try again — smaller pages finish before the throttle bites.
        pieces, ok = [], True
        for hour in (0, 6, 12, 18):
            a = f"{day.isoformat()}T{hour:02d}:00:00Z"
            nxt = hour + 6
            b = (f"{day.isoformat()}T{nxt:02d}:00:00Z" if nxt < 24
                 else f"{(day + dt.timedelta(days=1)).isoformat()}T00:00:00Z")
            part = fetch(a, b, tries=8)
            if part is None:
                ok = False
            else:
                pieces += part
        return pieces, (None if ok else day.isoformat())

    # StartJobRun needs a day per call — a thousand starts in one day would truncate a wider
    # window. CreateJob/UpdateJob are rare, so a week per call is safe and 7x fewer requests.
    step = 1 if event == "StartJobRun" else 7
    days, cur = [], start
    while cur < end:
        days.append(cur)
        cur += dt.timedelta(days=step)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, days))
    rows = [r for chunk, _ in results for r in chunk]
    failed = [d for _, d in results if d]
    if failed:
        say(f"  ! {event}: {len(failed)} day(s) could not be swept — numbers are a floor")
    return rows, failed


def scan_s3_archive(profile: str, region: str, bucket: str, account: str,
                    start: dt.date, end: dt.date, say) -> list[dict]:
    """Source 3 — the trail archive, for the window Event history has already dropped.
    Filters server-side with S3 Select; downloading the archive is ~11x slower."""
    sql = ("SELECT * FROM S3Object[*].Records[*] r "
           "WHERE r.eventSource = 'glue.amazonaws.com'")
    keys: list[str] = []
    cur = start
    while cur < end:
        prefix = f"AWSLogs/{account}/CloudTrail/{region}/{cur:%Y/%m/%d}/"
        token = None
        while True:
            args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
                    "--max-keys", "1000", "--no-paginate"]
            if token:
                args += ["--continuation-token", token]
            page = aws(profile, region, *args, allow_fail=True) or {}
            keys.extend(o["Key"] for o in page.get("Contents", []))
            token = page.get("NextContinuationToken")
            if not token:
                break
        cur += dt.timedelta(days=1)
    if not keys:
        say("  ! trail archive: no objects found for that window")
        return []
    say(f"  archive: {len(keys):,} objects to scan")

    client = None
    try:                      # one process-wide client beats 34k CLI subprocesses
        import boto3
        client = boto3.Session(profile_name=profile, region_name=region).client("s3")
        say("  archive: using boto3 for the scan")
    except Exception:
        say("  archive: boto3 not available — falling back to the CLI, this is much slower")

    def select(key: str) -> str:
        if client is not None:
            resp = client.select_object_content(
                Bucket=bucket, Key=key, ExpressionType="SQL", Expression=sql,
                InputSerialization={"JSON": {"Type": "DOCUMENT"}, "CompressionType": "GZIP"},
                OutputSerialization={"JSON": {"RecordDelimiter": "\n"}})
            buf = b""
            for event in resp["Payload"]:
                if "Records" in event:
                    buf += event["Records"]["Payload"]
            return buf.decode("utf-8", errors="replace")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            path = fh.name
        try:
            proc = subprocess.run(
                ["aws", "s3api", "select-object-content", "--bucket", bucket, "--key", key,
                 "--expression", sql, "--expression-type", "SQL",
                 "--input-serialization",
                 '{"JSON":{"Type":"DOCUMENT"},"CompressionType":"GZIP"}',
                 "--output-serialization", '{"JSON":{"RecordDelimiter":"\\n"}}',
                 "--profile", profile, "--region", region, path],
                capture_output=True, text=True)
            return Path(path).read_text(encoding="utf-8", errors="replace") if proc.returncode == 0 else ""
        finally:
            Path(path).unlink(missing_ok=True)

    def one(key: str) -> list[dict]:
        try:
            body = select(key)
        except Exception:
            return []
        out = []
        for line in body.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("eventName") not in ("StartJobRun", "CreateJob", "UpdateJob"):
                continue
            if rec.get("errorCode"):
                continue
            rp = rec.get("requestParameters") or {}
            re_ = rec.get("responseElements") or {}
            out.append({"t": rec["eventTime"], "ev": rec["eventName"],
                        "job": rp.get("jobName") or rp.get("name"),
                        "id": re_.get("jobRunId"), "rp": rp})
        return out

    with ThreadPoolExecutor(max_workers=48 if client is not None else 24) as pool:
        return [r for chunk in pool.map(one, keys) for r in chunk]


def sizing_timeline(events: list[dict], api_runs: list[dict]) -> tuple[dict, dict]:
    """Worker sizing is a timeline, not a value. A job resized mid-period must be priced
    per run at the configuration in force when that run actually started."""
    timeline: dict[str, list[tuple[dt.datetime, float]]] = collections.defaultdict(list)
    for e in events:
        if e["ev"] not in ("CreateJob", "UpdateJob"):
            continue
        rp = e.get("rp") or {}
        src = rp.get("jobUpdate") if e["ev"] == "UpdateJob" else rp
        job = rp.get("jobName") or rp.get("name")
        if not job or not isinstance(src, dict):
            continue
        worker, count = src.get("workerType"), src.get("numberOfWorkers")
        capacity = src.get("maxCapacity") or src.get("allocatedCapacity")
        dpu = DPU_OF.get(worker, 0) * (count or 0) if worker else (capacity or 0)
        if dpu:
            timeline[job].append((parse_ts(e["t"]), dpu))
    for job in timeline:
        timeline[job].sort()
    observed: dict[str, list[float]] = collections.defaultdict(list)
    for run in api_runs:
        dpu = run_dpu(run)
        if dpu:
            observed[run["job"]].append(dpu)
    return dict(timeline), dict(observed)


def dpu_at(job: str, when: dt.datetime, timeline: dict, observed: dict) -> float:
    events = timeline.get(job)
    if events:
        idx = bisect.bisect_right([t for t, _ in events], when) - 1
        return events[idx][1] if idx >= 0 else events[0][1]   # extrapolate earliest backwards
    seen = observed.get(job)
    return statistics.mode(seen) if seen else 2


def log_spans(profile: str, region: str, buckets: dict, say) -> dict:
    """Source 4 — stream names are the jobRunId and the Glue log groups keep retention
    'never', so streams outlive the job. Sample per (job, ISO week); querying every run
    rate-limits. Spans miss container startup, so add the validated offset."""
    def span(run_id: str) -> float | None:
        for group in ("/aws-glue/jobs/error", "/aws-glue/jobs/output"):
            cmd = ["aws", "logs", "describe-log-streams", "--log-group-name", group,
                   "--log-stream-name-prefix", run_id, "--limit", "10",
                   "--profile", profile, "--region", region, "--output", "json"]
            for attempt in range(4):
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode == 0:
                    best = None
                    for st in json.loads(proc.stdout or "{}").get("logStreams", []):
                        if "firstEventTimestamp" in st and "lastEventTimestamp" in st:
                            secs = (st["lastEventTimestamp"] - st["firstEventTimestamp"]) / 1000
                            best = secs if best is None else max(best, secs)
                    if best is not None:
                        return best
                    break
                time.sleep(1.5 * (attempt + 1))
        return None

    def median_for(item):
        key, ids = item
        vals = [v for v in (span(i) for i in ids[:SAMPLE_RUNS]) if v is not None]
        return key, (statistics.median(vals) if vals else None)

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(pool.map(median_for, buckets.items()))
    hit = sum(1 for v in found.values() if v is not None)
    say(f"  log spans: {hit}/{len(buckets)} (job, week) buckets resolved")
    return found


def reconstruct(profile: str, region: str, api_runs: list[dict], start: str, end: str,
                archive_bucket: str | None, account: str, say) -> list[dict]:
    """Return the runs the Glue API cannot see, priced and tagged by source."""
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    today = dt.datetime.now(dt.timezone.utc).date()
    lookback = max(d0, today - dt.timedelta(days=LOOKBACK_DAYS - 1))  # Event history reach

    events: list[dict] = []
    if lookback < d1:
        say(f"· CloudTrail Event history {lookback} → {d1}")
        for name in ("StartJobRun", "CreateJob", "UpdateJob"):
            rows, _ = sweep_cloudtrail(profile, region, name, lookback, d1, say)
            for r in rows:
                r["src"] = "ct"
            events += rows
    gap_days = (lookback - d0).days
    if gap_days > 0:
        if archive_bucket:
            say(f"· CloudTrail S3 archive {d0} → {lookback} ({gap_days} days beyond Event history)")
            rows = scan_s3_archive(profile, region, archive_bucket, account, d0, lookback, say)
            for r in rows:
                r["src"] = "s3"
            events += rows
        else:
            say(f"  ! {gap_days} days of the period predate Event history and no --archive-bucket "
                f"was given — that window stays Glue-API-only")

    api_ids = {r["id"] for r in api_runs if r.get("id")}
    if api_runs and not api_ids:
        # Without run ids every CloudTrail start looks missing and the total doubles.
        raise AwsError("Glue runs carry no Id, so CloudTrail events cannot be deduplicated "
                       "against them. Refusing to reconstruct — the result would double-count.")
    seen, missing = set(), []
    for e in events:
        if e["ev"] != "StartJobRun" or not e.get("id") or not e.get("job"):
            continue
        if e["id"] in api_ids or e["id"] in seen:
            continue
        if not start <= e["t"][:10] < end:
            continue
        seen.add(e["id"])
        missing.append(e)
    if not missing:
        say("  every billed run is already in the Glue API — nothing to reconstruct")
        return []
    say(f"  {len(missing):,} runs exist only in CloudTrail")

    timeline, observed = sizing_timeline(events, api_runs)
    # A weekly median is too coarse for a job that runs thousands of times: a day of short
    # runs inherits the week's long ones and the hours slosh between days. Sample those daily.
    volume = collections.Counter(e["job"] for e in missing)
    # 200 was too high: a job with 149 runs at 80 DPU moves more hours than one with
    # 4,000 at 3 DPU. Sample per day whenever a job has enough runs for a daily median
    # to mean anything, and let the cheap tail stay weekly.
    heavy = {job for job, count in volume.items() if count >= HEAVY_RUNS}
    buckets: dict = collections.defaultdict(list)
    for e in missing:
        when = parse_ts(e["t"])
        key = when.date().isoformat() if e["job"] in heavy else when.isocalendar()[:2]
        buckets[(e["job"], key)].append(e["id"])
    if heavy:
        say(f"  {len(heavy)} high-volume job(s) sampled per day rather than per week")
    spans = log_spans(profile, region, buckets, say)

    # A bucket whose log streams have aged out must not inherit the whole account's median —
    # a 60-second job would be priced like a 5-minute one. Fall back within the same job first.
    per_job_span: dict[str, list[float]] = collections.defaultdict(list)
    for (job, _), value in spans.items():
        if value is not None:
            per_job_span[job].append(value + SPAN_STARTUP)
    per_job_exec: dict[str, list[float]] = collections.defaultdict(list)
    for run in api_runs:
        if run.get("exec"):
            per_job_exec[run["job"]].append(run["exec"])
    known = [r["exec"] for r in api_runs if r.get("exec")]
    global_median = statistics.median(known) if known else 120
    guessed = 0

    def duration(job: str, key) -> tuple[float, bool]:
        raw = spans.get((job, key))
        if raw is not None:
            return raw + SPAN_STARTUP, False
        if per_job_span.get(job):
            return statistics.median(per_job_span[job]), True
        if per_job_exec.get(job):
            return statistics.median(per_job_exec[job]), True
        return global_median, True

    out = []
    for e in missing:
        when = parse_ts(e["t"])
        key = when.date().isoformat() if e["job"] in heavy else when.isocalendar()[:2]
        secs, inferred = duration(e["job"], key)
        guessed += inferred
        secs = max(secs, 60)
        out.append({"job": e["job"], "started": e["t"], "exec": int(secs),
                    "dpu_seconds": None, "worker": None, "workers": None,
                    "max_capacity": dpu_at(e["job"], when, timeline, observed),
                    "state": "ESTIMATED", "trigger": None, "error": "",
                    "src": e["src"], "estimated": True})
    if guessed:
        say(f"  {guessed:,} of {len(missing):,} reconstructed runs had no log stream left; "
            f"their duration is that job's own median")
    return out


# ---------------------------------------------------------------------- narrative

def n(value: float, places: int = 1) -> str:
    return f"{value:,.{places}f}"


def money(value: float) -> str:
    return f"${value:,.2f}"


def env_of(name: str) -> str:
    """Which environment a job name claims, looked for anywhere in the name.

    The markers are configured as prefixes because that is how most accounts write
    them, but plenty carry the marker in the middle or at the end: `prod_etl_daily`,
    `etl_prod_daily` and `daily_etl_prod` all mean the same thing. The separator is
    stripped and the bare token matched between name boundaries, so the token is
    found wherever it sits without reading `reproduction_etl` as production.
    """
    for prefix in ENV_PREFIXES:
        token = prefix.rstrip("_-.")
        if token and re.search(r"(?:^|[_.-])" + re.escape(token) + r"(?:$|[_.-])",
                               name, re.IGNORECASE):
            return token
    return "legacy"


def month_label(key: str) -> str:
    return dt.datetime.strptime(key, "%Y-%m").strftime("%B %Y")


def find_duplicates(by_job: dict[str, float]) -> list[tuple[str, float, str, float]]:
    """Legacy jobs still running beside a production twin of the same name.

    The first configured prefix is taken as the production one, because that is the
    order every convention of this shape is written in.
    """
    pairs = []
    prod = ENV_PREFIXES[0] if ENV_PREFIXES else ""
    for name, spend in by_job.items():
        if env_of(name) != "legacy" or not prod:
            continue
        for twin in (f"{prod}{name}", f"{prod}{name[0].lower()}{name[1:]}"):
            if twin in by_job and spend > by_job[twin]:
                pairs.append((name, spend, twin, by_job[twin]))
                break
    return sorted(pairs, key=lambda p: -p[1])


def find_resizes(window: list[dict]) -> list[tuple[str, list[str]]]:
    """Jobs whose worker configuration changed between runs inside the period."""
    seen: dict[str, list[tuple[str, tuple]]] = collections.defaultdict(list)
    for run in sorted(window, key=lambda r: r["started"]):
        config = (run.get("worker"), run.get("workers"))
        if seen[run["job"]] and seen[run["job"]][-1][1] == config:
            continue
        seen[run["job"]].append((run["started"][:10], config))
    changed = [(job, steps) for job, steps in seen.items() if len(steps) > 1]
    out = []
    for job, steps in sorted(changed, key=lambda kv: -len(kv[1])):
        out.append((job, [f"{DPU_OF.get(c[0], 0) * (c[1] or 0):.0f}" for _, c in steps]))
    return out


def build_findings(data: dict, jobs: list[dict], triggers: dict) -> str:
    """Only the findings this account actually has. No empty sections."""
    rate, by_job, window = data["rate"], data["by_job"], data["window"]
    total = data["attributed"] or 1.0
    cards: list[tuple[str, str, str]] = []

    by_env: dict[str, float] = collections.defaultdict(float)
    for name, spend in by_job.items():
        by_env[env_of(name)] += spend

    legacy = by_env.get("legacy", 0.0)
    dupes = find_duplicates(by_job)
    # Only meaningful where the convention is actually in use. An account where every job
    # is "legacy" simply does not name jobs that way, and reporting 100% is noise.
    uses_convention = sum(1 for name in by_job if env_of(name) != "legacy") >= 0.2 * len(by_job)
    if uses_convention and 0.05 < legacy / total < 0.90:
        extra = ""
        if dupes:
            name, spend, twin, twin_spend = dupes[0]
            ratio = spend / twin_spend if twin_spend else 0
            extra = (f" <code>{name}</code> alone spent {n(spend)} DPU-h against "
                     f"{n(twin_spend)} for <code>{twin}</code> — {ratio:.1f}× for the same work.")
        cards.append((
            "Jobs with no environment marker", money(legacy * rate),
            f"Jobs whose name carries none of "
            f"{'/'.join(f'<code>{p}</code>' for p in ENV_PREFIXES)} anywhere in it "
            f"account for <b>{n(legacy)} DPU-h, {legacy / total * 100:.1f}%</b> of attributed spend."
            f"{extra} These are usually the safest savings in the account: confirm the replacement "
            f"runs, then delete."))

    waste = sum(run_hours(r) for r in window if r.get("state") in BAD_STATES)
    bad_n = sum(1 for r in window if r.get("state") in BAD_STATES)
    if bad_n:
        worst = collections.Counter()
        for run in window:
            if run.get("state") in BAD_STATES:
                worst[run["job"]] += run_hours(run)
        job, burned = worst.most_common(1)[0]
        cards.append((
            "Runs that were billed and produced nothing", money(waste * rate),
            f"{bad_n} runs ended in <code>FAILED</code>, <code>TIMEOUT</code> or "
            f"<code>STOPPED</code> and were billed anyway: {n(waste)} DPU-h. "
            f"The worst is <code>{job}</code> at {money(burned * rate)}."))

    untriggered = {j: v for j, v in by_job.items() if j not in triggers and v > 0}
    untriggered_share = sum(untriggered.values()) / total if untriggered else 0.0
    sandbox = by_env.get("sandbox", 0.0)
    if untriggered_share > 0.60 and sandbox <= 0:
        # Not a governance finding — this account orchestrates Glue from somewhere else.
        cards.append((
            "Glue triggers are not how work starts here", f"{untriggered_share * 100:.0f}% of spend",
            "Almost nothing in this account is started by a Glue trigger, so the schedule behind "
            "each job lives outside Glue — Step Functions, Airflow, or a person. That is a "
            "reporting limit, not a problem: this data cannot tell you what cadence a job runs on, "
            "only that it ran."))
    elif sandbox > 0 or (untriggered and 0.03 < untriggered_share <= 0.60):
        # "sandbox" only exists if this account's convention has such a prefix.
        use_sandbox = sandbox > 0 and any(env_of_prefix == "sandbox"
                                          for env_of_prefix in ENV_NAMES)
        pool = ({j: v for j, v in by_job.items() if env_of(j) == "sandbox"}
                if use_sandbox else untriggered)
        spend = sum(pool.values())
        label = ("%s* jobs" % ENV_PREFIX_BY_NAME.get("sandbox", "sandbox_")
                 if use_sandbox else "jobs with no Glue trigger")
        top = max(pool, key=pool.get)
        cards.append((
            "Ungoverned ad-hoc work", money(spend * rate),
            f"{label} account for {n(spend)} DPU-h. <code>{top}</code> is the heaviest. "
            f"Nothing schedules these and nothing watches them — they are somebody's "
            f"working copy running on the production bill."))

    resizes = find_resizes(window)
    if resizes:
        job, steps = resizes[0]
        cards.append((
            "Silent resizes", f"{len(resizes)} jobs",
            f"<code>{job}</code> changed worker size {len(steps) - 1} times inside the period "
            f"({' → '.join(steps)} DPU). Somebody is hand-tuning while it runs. This is also why "
            f"a single DPU value per job misprices every run on the far side of a change — each "
            f"run here is priced at the configuration it actually used."))

    if data["crawler"] > 0.005:
        cards.append((
            "Crawlers are a separate line", money(data["crawler"]),
            f"{n(data['crawler_h'])} DPU-h under the crawler usage type, outside this attribution "
            f"and outside the inventory below. Worth its own pass: scheduled crawlers over tables "
            f"already in the catalog are usually pure waste."))

    cards.append((
        "What this data cannot show", "",
        "<code>GetJobRuns</code> history dies with the job. A job deleted inside the period keeps "
        "its cost on the bill and leaves no runs here. The tell is a spiky residual rather than a "
        "flat one — see <code>references/service-deep-dive.md</code> for the CloudTrail "
        "reconstruction if coverage ever drops below 90%."))

    return "\n".join(
        f'<div class="f"><h4>{title}'
        + (f' <span class="amt">{amount}</span>' if amount else "")
        + f'</h4><p>{body}</p></div>'
        for title, amount, body in cards)


def build_notes(data: dict, runs: list[dict]) -> dict[str, str]:
    """The sentences under each figure, written from what the numbers actually say."""
    rate, window = data["rate"], data["window"]
    total = data["attributed"] or 1.0
    covs = data["covs"]
    resid = data["bill_qty"] - data["attributed"]
    days = max(len(data["bill_day"]), 1)
    coverage = data["attributed"] / data["bill_qty"] * 100 if data["bill_qty"] else 0

    spiky = False
    if covs:
        spread = statistics.pstdev(covs) if len(covs) > 1 else 0
        spiky = spread > 0.12
    resid_shape = (
        "is spiky rather than flat, which points at billed work this attribution cannot "
        "see \u2014 most often a job deleted inside the period."
        if spiky else
        "is flat and has no spikes, which is the per-run provisioning overhead that "
        "<code>DPUSeconds</code> does not capture.")

    if data["anomalies"]:
        parts = []
        for day, billed, attributed in data["anomalies"]:
            pct = attributed / billed * 100 if billed else 0
            parts.append(f"{day} sits at {pct:.0f}% ({n(billed)} billed vs {n(attributed)} attributed)")
        over = [d for d, b, a in data["anomalies"] if b and a / b > 1]
        under = [d for d, b, a in data["anomalies"] if b and a / b < 1]
        # A day that overshoots while its neighbours reconcile is hours moving between days,
        # not hours invented. Check the surrounding window before blaming the method.
        window_ok = True
        for day, _, _ in data["anomalies"]:
            base = dt.date.fromisoformat(day)
            span = [(base + dt.timedelta(days=k)).isoformat() for k in range(-3, 4)]
            billed = sum(data["bill_day"].get(x, 0.0) for x in span)
            got = sum(data["by_day"].get(x, 0.0) for x in span)
            if not (billed and 0.9 <= got / billed <= 1.1):
                window_ok = False
        # An over-attributed day made of reconstructed hours is a sampled-duration artefact.
        # One made of Glue API hours can only be AWS posting them on another calendar day.
        est_driven = any(
            (data["by_day_src"].get(day, {}).get("ct", 0.0)
             + data["by_day_src"].get(day, {}).get("s3", 0.0))
            > data["by_day_src"].get(day, {}).get("api", 0.0)
            for day, b, a in data["anomalies"] if b and a / b > 1)
        if over and est_driven:
            tail = (". The overshooting days are built mostly from reconstructed runs, so this is "
                    "the sampled median duration spreading a busy day's work across its "
                    "neighbours — not hours invented. Read those days as a week, not a day.")
        elif over and window_ok:
            tail = (". Their surrounding week reconciles, so these are hours moving between "
                    "days rather than hours invented — either AWS posting usage on a different "
                    "calendar day than the run started, or, for reconstructed runs, a sampled "
                    "median duration spreading a busy day's work across its neighbours.")
        elif over and under:
            tail = (". A day over 100% next to a day under it is almost always AWS posting the "
                    "usage on a different calendar day than the run started — check that the "
                    "window reconciles rather than the single day.")
        elif over:
            tail = (". Attributing more than was billed is a bug, not rounding: check the "
                    "timestamps for a timezone that was never normalised to UTC.")
        else:
            tail = (". These days are missing attribution, not misdated — the runs that produced "
                    "the usage are simply not in the API. That is the signature of a job deleted "
                    "inside the period, and it is what the CloudTrail reconstruction recovers.")
        note = ("The flagged days are worth reading before the totals: " + "; ".join(parts) + tail)
    else:
        note = "No single day fell outside the 85–115% band, so no day needs an explanation."

    peak = max(data["bill_month"], key=lambda m: data["bill_month"][m]) if data["bill_month"] else None
    monthly = (f"{month_label(peak)} is the peak at {n(data['bill_month'][peak])} DPU-h. "
               f"Months are billed as AWS posts them, so a partial first or last month reads low."
               if peak else "")

    short = [r for r in window if (r.get("exec") or 0) < 60]
    short_h = sum(run_hours(r) - run_dpu(r) * (r.get("exec") or 0) / 3600 for r in short)
    secs = [r["exec"] for r in window if (r.get("exec") or 0) > 0]
    median = statistics.median(secs) if secs else 0
    short_pct = len(short) / len(window) * 100 if window else 0
    short_spend = sum(run_hours(r) for r in short) / total * 100
    if short_pct > 25 and short_spend > 15:
        duration = (f"<b>This account pays a short-run penalty.</b> {short_pct:.0f}% of runs finish "
                    f"in under a minute and Glue bills a 1-minute minimum for each, costing "
                    f"{money(short_h * rate)} in floor alone. The median run is {median:.0f} s. "
                    f"The fix is batching those runs together, not resizing them.")
    else:
        duration = (f"<b>There is no short-run penalty here.</b> Only {short_pct:.1f}% of runs finish "
                    f"in under a minute; the 1-minute billing floor costs {money(short_h * rate)} "
                    f"across the whole period. The median run is {median:.0f} s, so the savings are "
                    f"in which jobs run, not in how they are grouped.")

    ranked = sorted(data["by_job"].items(), key=lambda kv: -kv[1])
    dupes = find_duplicates(data["by_job"])
    if ranked:
        job, spend = ranked[0]
        if dupes and dupes[0][0] == job:
            _, _, twin, twin_spend = dupes[0]
            top = (f"<b>The most expensive job in the account is a legacy duplicate.</b> "
                   f"<code>{job}</code> burned {n(spend)} DPU-h ({money(spend * rate)}) doing the "
                   f"same work as <code>{twin}</code>, which spent {n(twin_spend)} DPU-h "
                   f"({money(twin_spend * rate)}).")
        else:
            top = (f"<code>{job}</code> is the single largest consumer at {n(spend)} DPU-h "
                   f"({money(spend * rate)}), {spend / total * 100:.1f}% of attributed spend across "
                   f"{data['job_runs'][job]:,} runs.")
    else:
        top = "No job ran inside this period."

    return {
        "RESID_SHAPE": resid_shape,
        "RESID_SHORT": f"≈ {resid / days:.2f} / day, {'spiky' if spiky else 'flat'}",
        "RESID_VERDICT": ("The residual is spiky — treat the per-job numbers as a floor and run "
                          "the CloudTrail reconstruction."
                          if spiky or coverage < 90 else
                          f"The flat {100 - coverage:.1f}% residual says there are none."),
        "DELETED_NOTE": ("only if jobs were deleted — the residual suggests some were"
                         if spiky or coverage < 90 else
                         "only if jobs were deleted — none in this period"),
        "COV_MED": f"{statistics.median(covs) * 100:.1f}" if covs else "—",
        "COV_P10": f"{sorted(covs)[len(covs) // 10] * 100:.1f}" if covs else "—",
        "COV_P90": f"{sorted(covs)[len(covs) * 9 // 10] * 100:.1f}" if covs else "—",
        "ANOMALY_NOTE": note,
        "MONTHLY_NOTE": monthly,
        "DURATION_NOTE": duration,
        "TOP_NOTE": top,
        "DUR_AXIS_NOTE": (f"median {median:.0f} s · billing floor = {money(short_h * rate)} "
                          f"for the period"),
    }


# ------------------------------------------------------------------------ template

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>(function(){var t="light";try{t=localStorage.getItem("glue-theme")||"light";}catch(e){}document.documentElement.setAttribute("data-theme",t);})();</script>
<title>Glue cost · {{PROFILE}} · {{PERIOD}}</title>
<style>
/* No webfont link: this file gets emailed and read offline, and a cost report about
   someone's account should not tell a font CDN who is reading it. IBM Plex is used
   when the reader already has it; otherwise these fall back to the system stack. */
:root{
  --ground:#eef1f2; --surface:#ffffff; --surface-2:#f7f9f9; --rule:#dbe2e3; --rule-soft:#e8edee;
  --ink:#101719; --ink-2:#435357; --ink-3:#71838a;
  --accent:#0d6a66; --accent-soft:#d7ebe9;
  --ok:#2c6f4c; --ok-bg:#dceee3;
  --warn:#8a5c0c; --warn-bg:#f6e8cd;
  --crit:#96322a; --crit-bg:#f7dedb;
  --idle:#6a777b; --idle-bg:#e4e9ea;
  --bar:#9ec9c6;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --ground:#0c1113; --surface:#131a1d; --surface-2:#171f22; --rule:#263134; --rule-soft:#1e282b;
  --ink:#e6eeef; --ink-2:#a3b4b8; --ink-3:#7d8f94;
  --accent:#5cb8b1; --accent-soft:#12332f;
  --ok:#7fc79c; --ok-bg:#16301f;
  --warn:#dcac54; --warn-bg:#332612;
  --crit:#e5897d; --crit-bg:#371814;
  --idle:#8b999d; --idle-bg:#1e2629;
  --bar:#2f6360;
}}
:root[data-theme="dark"]{
  --ground:#0c1113; --surface:#131a1d; --surface-2:#171f22; --rule:#263134; --rule-soft:#1e282b;
  --ink:#e6eeef; --ink-2:#a3b4b8; --ink-3:#7d8f94;
  --accent:#5cb8b1; --accent-soft:#12332f;
  --ok:#7fc79c; --ok-bg:#16301f;
  --warn:#dcac54; --warn-bg:#332612;
  --crit:#e5897d; --crit-bg:#371814;
  --idle:#8b999d; --idle-bg:#1e2629;
  --bar:#2f6360;
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:14px;line-height:1.45;-webkit-font-smoothing:antialiased}
.wrap{max-width:1420px;margin:0 auto;padding:32px 24px 64px}

/* ---------- theme toggle ---------- */
.tt{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
  padding:0;border:1px solid var(--rule);border-radius:2px;background:var(--surface);
  color:var(--ink-2);cursor:pointer}
.tt:hover{border-color:var(--accent);color:var(--accent)}
.tt:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tt svg{width:17px;height:17px;display:block}
/* show the icon for the theme the click leads to, not the one already on screen */
:root[data-theme="dark"] .tt .ico-moon{display:none}
:root:not([data-theme="dark"]) .tt .ico-sun{display:none}

/* ---- masthead ---- */
.mast{display:flex;flex-wrap:wrap;align-items:flex-end;gap:24px;justify-content:space-between;
  padding-bottom:18px;border-bottom:2px solid var(--ink)}
.mast h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.02em;text-wrap:balance}
.mast .sub{margin:6px 0 0;color:var(--ink-2);font-size:13px}
.acct{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink-3);
  text-align:right;line-height:1.7;white-space:nowrap}

/* ---- stat strip ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin:0 0 26px;border-top:none}
.stat{background:var(--surface);padding:14px 16px}
.stat .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink-3);font-weight:600}
.stat .v{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:24px;font-weight:600;
  letter-spacing:-.02em;margin-top:4px;font-variant-numeric:tabular-nums}
.stat .n{font-size:11.5px;color:var(--ink-3);margin-top:2px}
.stat.hl .v{color:var(--accent)}

/* ---- controls ---- */
.bar{position:sticky;top:0;z-index:30;background:var(--ground);
  padding:12px 0 12px;border-bottom:1px solid var(--rule);
  display:flex;flex-wrap:wrap;gap:14px 20px;align-items:center}
.grp{display:flex;align-items:center;gap:7px}
.grp>.lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink-3);font-weight:600}
input[type=search]{font:inherit;font-size:13px;background:var(--surface);color:var(--ink);
  border:1px solid var(--rule);border-radius:2px;padding:6px 10px;width:250px;
  font-family:"IBM Plex Mono",ui-monospace,monospace}
input[type=search]:focus-visible,button:focus-visible,th:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.chip{font:inherit;font-size:12px;font-weight:500;cursor:pointer;background:var(--surface);
  color:var(--ink-2);border:1px solid var(--rule);border-radius:2px;padding:5px 11px;
  font-family:"IBM Plex Mono",ui-monospace,monospace}
.chip:hover{border-color:var(--accent);color:var(--ink)}
.chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--surface);font-weight:600}
:root[data-theme="dark"] .chip[aria-pressed="true"],
:root:not([data-theme="light"]) .chip[aria-pressed="true"]{color:#0c1113}
.count{margin-left:auto;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;color:var(--ink-3);
  font-variant-numeric:tabular-nums}

/* ---- table ---- */
.tw{overflow:auto;max-height:min(74vh,880px);overscroll-behavior:contain;
  border:1px solid var(--rule);background:var(--surface)}
table{border-collapse:collapse;width:100%;min-width:1080px}
thead th{position:sticky;top:0;z-index:10;background:var(--surface-2);
  font-size:10.5px;text-transform:uppercase;letter-spacing:.075em;font-weight:600;color:var(--ink-2);
  text-align:left;padding:9px 12px;box-shadow:inset 0 -1px 0 var(--rule);cursor:pointer;white-space:nowrap;
  user-select:none}
thead th:hover{color:var(--accent)}
thead th.num{text-align:right}
thead th .ar{color:var(--accent);font-size:9px;margin-left:3px}
tbody td{padding:7px 12px;border-bottom:1px solid var(--rule-soft);vertical-align:middle;white-space:nowrap}
tbody tr:hover{background:var(--surface-2)}
td.num{text-align:right;font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;font-size:12.5px}
td.job{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12.5px;font-weight:500;
  border-left:3px solid transparent;padding-left:12px}
tr[data-act="stale"] td.job{border-left-color:var(--warn)}
tr[data-act="never"] td.job{border-left-color:var(--idle)}
tr[data-act="live"]  td.job{border-left-color:var(--accent)}
.env{font-size:10px;font-weight:600;letter-spacing:.05em;color:var(--ink-3);text-transform:uppercase}
.pill{display:inline-block;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px;
  font-weight:600;padding:2px 7px;border-radius:2px;letter-spacing:.02em}
.p-ok{background:var(--ok-bg);color:var(--ok)} .p-crit{background:var(--crit-bg);color:var(--crit)}
.p-warn{background:var(--warn-bg);color:var(--warn)} .p-idle{background:var(--idle-bg);color:var(--idle)}
/* Glue versions read as a scale, because a higher one is simply better: the newest
   version in the account is green, the oldest red, anything between it amber. */
.vnew{background:var(--ok-bg);color:var(--ok)} .vmid{background:var(--warn-bg);color:var(--warn)}
.vold{background:var(--crit-bg);color:var(--crit)}
td.worker{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink-2)}
td.when{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink-2)}
td.when .ago{color:var(--ink-3)}
.dpu{display:flex;align-items:center;justify-content:flex-end;gap:8px}
.dpu .track{width:52px;height:4px;background:var(--rule);position:relative;flex:none}
.dpu .fill{position:absolute;inset:0 auto 0 0;background:var(--bar)}
.flagdot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--rule)}
.flagdot.on{background:var(--accent)}
.empty{padding:36px;text-align:center;color:var(--ink-3);font-size:13px}

/* ---- notes ---- */
.notes{margin-top:30px;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:26px}
.notes h2{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--ink-3);
  margin:0 0 8px;font-weight:600;padding-bottom:6px;border-bottom:1px solid var(--rule)}
.notes p{margin:0 0 9px;color:var(--ink-2);font-size:12.5px;max-width:62ch}
.notes code{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;
  background:var(--surface);border:1px solid var(--rule);padding:1px 4px;border-radius:2px;color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:4px}
.legend span{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2)}
.legend i{width:3px;height:14px;display:inline-block}
@media (max-width:640px){ .wrap{padding:20px 12px 48px} .mast h1{font-size:21px} .acct{text-align:left} }
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
<style>
/* ---------- deep dive ---------- */
.dd{margin:34px 0 40px}
.sec-h{display:flex;align-items:baseline;gap:14px;border-bottom:2px solid var(--ink);
  padding-bottom:10px;margin:0 0 16px;flex-wrap:wrap}
.sec-h h2{margin:0;font-size:19px;font-weight:700;letter-spacing:-.015em}
.sec-h .tag{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--ink-3);
  text-transform:uppercase;letter-spacing:.08em}
.lead{max-width:66ch;color:var(--ink-2);font-size:13.5px;margin:0 0 18px}
.lead strong{color:var(--ink);font-weight:600}
.recon{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin-bottom:26px}

figure{margin:0}
/* Captions and chart subtitles run the width of the thing they explain. A measure
   cap belongs on running prose, not on a sentence that reads as part of a figure —
   half-width under a full-width chart just looks like it stopped early. */
figcaption{font-size:12px;color:var(--ink-3);margin-top:9px;line-height:1.5}
figcaption b{color:var(--ink-2);font-weight:600}
.card{background:var(--surface);border:1px solid var(--rule);padding:18px 18px 16px;margin-bottom:20px}
.card > h3{margin:0 0 3px;font-size:14px;font-weight:600;letter-spacing:-.01em}
.card > .cs{margin:0 0 14px;font-size:12px;color:var(--ink-3)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:20px}
.grid2 .card{margin-bottom:0}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
.dia{width:100%;height:auto;display:block;max-width:1000px;margin:0 auto;color:var(--ink-2)}
.legend2{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 12px;font-size:11.5px;color:var(--ink-2)}
.legend2 span{display:flex;align-items:center;gap:6px}
.legend2 i{width:11px;height:11px;border-radius:2px;flex:none}
.legend2 i.ln{height:3px;width:16px;border-radius:0}
.ax{font-size:10px;fill:var(--ink-3);font-family:"IBM Plex Mono",ui-monospace,monospace}
.axl{font-size:10.5px;fill:var(--ink-3)}
.gl{stroke:var(--rule-soft);stroke-width:1}
.bl{stroke:var(--rule);stroke-width:1}
.vlab{font-size:10.5px;fill:var(--ink-2);font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-variant-numeric:tabular-nums}
.jlab{font-size:11px;fill:var(--ink);font-family:"IBM Plex Mono",ui-monospace,monospace}
.anno{font-size:10.5px;fill:var(--ink-2)}
.annoline{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:2 2}
#tip{position:fixed;z-index:100;pointer-events:none;opacity:0;transition:opacity .1s;
  background:var(--surface);border:1px solid var(--rule);box-shadow:0 4px 14px rgba(0,0,0,.13);
  padding:8px 10px;font-size:11.5px;line-height:1.5;max-width:270px;border-radius:2px}
#tip .t{font-weight:600;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;
  margin-bottom:3px;color:var(--ink)}
#tip .r{display:flex;justify-content:space-between;gap:16px;color:var(--ink-2);
  font-variant-numeric:tabular-nums}
#tip .r b{font-family:"IBM Plex Mono",ui-monospace,monospace;font-weight:600;color:var(--ink)}
.find{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:0 28px}
.find .f{border-top:1px solid var(--rule);padding:12px 0 4px}
.find h4{margin:0 0 5px;font-size:12.5px;font-weight:600;display:flex;gap:8px;align-items:baseline}
.find h4 .amt{font-family:"IBM Plex Mono",ui-monospace,monospace;color:var(--accent);font-size:12px}
.find p{margin:0 0 8px;font-size:12.5px;color:var(--ink-2);line-height:1.5}
.find code{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink)}
:root{ --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300;
       --sOther:#9a9a94; --sResid:#d03b3b;
       --o1:#86b6ef; --o2:#5598e7; --o3:#2a78d6; --o4:#184f95; --o5:#0d366b; }
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
  --sOther:#7d7d78; --sResid:#d03b3b;
  --o1:#256abf; --o2:#3987e5; --o3:#6da7ec; --o4:#9ec5f4; --o5:#cde2fb; }}
:root[data-theme="dark"]{
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
  --sOther:#7d7d78; --sResid:#d03b3b;
  --o1:#256abf; --o2:#3987e5; --o3:#6da7ec; --o4:#9ec5f4; --o5:#cde2fb; }
</style>
<style>
/* ---------- collapsible documentation ---------- */
details.howto{margin:34px 0 40px}
details.howto > summary{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;cursor:pointer;
  list-style:none;border-bottom:2px solid var(--ink);padding-bottom:10px}
details.howto > summary::-webkit-details-marker{display:none}
details.howto > summary::marker{content:""}
details.howto > summary h2{margin:0;font-size:19px;font-weight:700;letter-spacing:-.015em}
details.howto > summary:hover h2{color:var(--accent)}
details.howto > summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
details.howto > summary .chev{margin-left:auto;font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}
details.howto > summary .chev::after{content:"show ▾"}
details.howto[open] > summary .chev::after{content:"hide ▴"}
details.howto > summary:hover .chev{color:var(--accent)}
details.howto[open] > summary{margin-bottom:18px}

/* ---------- detail view ---------- */
tbody tr{cursor:pointer}
tbody tr:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
td.job .arrow{color:var(--ink-3);margin-left:7px;opacity:0;font-size:11px}
tbody tr:hover td.job .arrow{opacity:1;color:var(--accent)}
.back{display:inline-flex;align-items:center;gap:7px;background:none;border:none;cursor:pointer;
  font:inherit;font-size:12px;color:var(--ink-2);padding:0;margin-bottom:16px;
  font-family:"IBM Plex Mono",ui-monospace,monospace}
.back:hover{color:var(--accent)}
.dh{border-bottom:2px solid var(--ink);padding-bottom:14px;margin-bottom:0}
.dh h1{margin:0 0 8px;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:22px;
  font-weight:600;letter-spacing:-.02em;word-break:break-all;line-height:1.25}
.dh .chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.dh .desc{margin:10px 0 0;font-size:12.5px;color:var(--ink-2);max-width:70ch}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);border-top:none;margin-bottom:24px}
.spec{display:grid;grid-template-columns:auto 1fr;gap:6px 18px;font-size:12.5px;align-items:baseline}
.spec dt{color:var(--ink-3);font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
  font-weight:600;white-space:nowrap;padding-top:1px}
.spec dd{margin:0;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;
  word-break:break-all;color:var(--ink)}
.spec dd.off{color:var(--ink-3)}
.trg{border-top:1px solid var(--rule-soft);padding:9px 0;display:flex;flex-wrap:wrap;
  gap:4px 14px;align-items:baseline;font-size:12.5px}
.trg:first-of-type{border-top:none}
.trg .nm{font-family:"IBM Plex Mono",ui-monospace,monospace;font-weight:500}
.trg .mt{color:var(--ink-3);font-size:11.5px;font-family:"IBM Plex Mono",ui-monospace,monospace}
.errrow{border-top:1px solid var(--rule-soft);padding:9px 0;display:flex;gap:12px;align-items:flex-start}
.errrow:first-of-type{border-top:none}
.errrow .n{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;font-weight:600;
  color:var(--crit);background:var(--crit-bg);padding:1px 7px;border-radius:2px;flex:none}
.errrow .m{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;color:var(--ink-2);
  line-height:1.5;word-break:break-word}
.rt td.err{max-width:280px;white-space:normal;font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11px;color:var(--crit);line-height:1.4}
.rt td{white-space:nowrap}
.more{font:inherit;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;cursor:pointer;
  background:var(--surface);border:1px solid var(--rule);color:var(--ink-2);padding:7px 14px;
  border-radius:2px;margin-top:12px}
.more:hover{border-color:var(--accent);color:var(--ink)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
#detailView .rt thead th{background:var(--surface-2)}
</style>
</head>
<body>
<div class="wrap">

<div id="listView">

<header class="mast">
  <div>
    <h1>Glue Job Inventory</h1>
    <p class="sub">{{N_JOBS}} jobs · Glue spend attributed to named jobs, plus an inventory with version, last run and DPU per execution</p>
  </div>
  <div>
    <div class="acct">
      account {{ACCOUNT}} · {{REGION}}<br>
      profile {{PROFILE}}<br>
      snapshot {{SNAPSHOT}}
    </div>
    <button class="tt" type="button" id="themeBtn" style="margin-top:10px" title="Switch theme">
      <svg class="ico-moon" viewBox="0 0 20 20" aria-hidden="true">
        <path d="M17 12.3A7.4 7.4 0 0 1 7.7 3 7.4 7.4 0 1 0 17 12.3z" fill="currentColor"/>
      </svg>
      <svg class="ico-sun" viewBox="0 0 20 20" aria-hidden="true">
        <circle cx="10" cy="10" r="3.5" fill="currentColor"/>
        <g stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
          <path d="M10 1.7v2M10 16.3v2M1.7 10h2M16.3 10h2M4.2 4.2l1.4 1.4M14.4 14.4l1.4 1.4M15.8 4.2l-1.4 1.4M5.6 14.4l-1.4 1.4"/>
        </g>
      </svg>
    </button>
  </div>
</header>

<section class="stats" id="stats"></section>

<section class="dd">
  <div class="sec-h">
    <h2>Spend by job</h2>
    <span class="tag">{{PERIOD}} · {{USAGE_TYPE}} · ${{BILL_USD}} billed</span>
  </div>

  <div class="recon" id="recon"></div>

  <div class="card">
    <h3>Daily reconciliation</h3>
    <p class="cs">Billed against attributed DPU-hours, day by day. Daily granularity is what exposes where
    attribution breaks; the monthly total hides it.</p>
    <div class="legend2" id="lg1"></div>
    <svg class="chart" id="c1" viewBox="0 0 900 280" role="img"
      aria-label="Daily series of billed and attributed DPU-hours over {{PERIOD}}."></svg>
    <figcaption><b>Median daily coverage: {{COV_MED}}%</b> (p10 {{COV_P10}}%, p90 {{COV_P90}}%). {{ANOMALY_NOTE}}</figcaption>
  </div>

  <div class="grid2">
    <div class="card">
      <h3>Monthly composition</h3>
      <p class="cs">The six most expensive jobs, everything else grouped, and the unattributed residual.</p>
      <div class="legend2" id="lg2"></div>
      <svg class="chart" id="c2" viewBox="0 0 440 300" role="img"
        aria-label="Stacked bars of DPU-hours per month over {{PERIOD}}, broken out into the six most expensive jobs, the rest grouped, and the residual."></svg>
      <figcaption>{{MONTHLY_NOTE}}</figcaption>
    </div>

    <div class="card">
      <h3>Run duration</h3>
      <p class="cs">Share of runs and share of spend by duration band. This is the check for whether Glue's
      1-minute billing floor is inflating the bill.</p>
      <div class="legend2" id="lg4"></div>
      <svg class="chart" id="c4" viewBox="0 0 440 300" role="img"
        aria-label="Paired bars of the percentage of runs and the percentage of DPU-hours in each duration band."></svg>
      <figcaption>{{DURATION_NOTE}}</figcaption>
    </div>
  </div>

  <div class="card">
    <h3>The 15 most expensive jobs</h3>
    <p class="cs">DPU-hours per job, segmented by month. Hover for run counts and cost.</p>
    <div class="legend2" id="lg3"></div>
    <svg class="chart" id="c3" viewBox="0 0 900 520" role="img"
      aria-label="Horizontal bars of the 15 Glue jobs with the most DPU-hours over {{PERIOD}}, each bar segmented by month."></svg>
    <figcaption>{{TOP_NOTE}}</figcaption>
  </div>

</section>

<div class="sec-h"><h2>Job inventory</h2><span class="tag">{{INVENTORY_TAG}}</span></div>

<div class="bar">
  <div class="grp"><input type="search" id="q" placeholder="search jobs…" aria-label="Search jobs"></div>
  <div class="grp"><span class="lbl">Glue</span><span id="fv"></span></div>
  <div class="grp"><span class="lbl">Environment</span><span id="fe"></span></div>
  <div class="grp"><span class="lbl">Activity</span><span id="fa"></span></div>
  <span class="count" id="count"></span>
</div>

<div class="tw">
  <table>
    <thead><tr id="hrow"></tr></thead>
    <tbody id="tb"></tbody>
  </table>
  <div class="empty" id="empty" hidden>No jobs match those filters.</div>
</div>

<div class="legend" style="margin-top:14px">
  <span><i style="background:var(--accent)"></i>Active — ran within the last {{STALE_DAYS}} days</span>
  <span><i style="background:var(--warn)"></i>Stale — no run in over {{STALE_DAYS}} days</span>
  <span><i style="background:var(--idle)"></i>Never — no run on record</span>
  <span style="color:var(--ink-3)">Click a row for that job’s full history</span>
</div>

<details class="howto" id="howitworks">
  <summary>
    <h2>How this works</h2>
    <span class="tag">method · coverage · caveats</span>
    <span class="chev" aria-hidden="true"></span>
  </summary>

  <div class="card">
    <h3>Where the Glue spend goes</h3>
    <p class="cs">Cost Explorer stops at <code>SERVICE</code> and <code>USAGE_TYPE</code>: it knows Glue
    cost ${{BILL_USD}}, not which job spent it. This dashboard pushes through to the job. The run
    inventory comes from paginated <code>GetJobRuns</code> across all {{N_JOBS}} jobs;
    <strong>{{N_DPUS}} of {{N_RUNS}} runs carry <code>DPUSeconds</code></strong>, which is the figure AWS
    bills, so the attribution is measured rather than estimated.
    <strong>Coverage: {{COVERAGE}}%</strong> of billed DPU-hours land on a named job. The residual —
    {{RESIDUAL}} DPU-h, a median of {{RESID_DAY}} DPU-h per day — {{RESID_SHAPE}}</p>
  </div>

  <div class="card">
    <h3>How the attribution works, and why it reconciles</h3>
    <p class="cs">The Glue API only returns runs for jobs that still exist: delete a job and its cost stays on the
    bill while its history disappears. The CloudTrail fallback path exists for that case. It was not needed here.</p>
    <figure>
      <svg class="dia" viewBox="0 0 1000 392" role="img" aria-label="Diagram: runs from GetJobRuns split into those carrying DPUSeconds and those that do not; both sum to {{ATTR}} attributed DPU-hours, compared against the {{BILL}} DPU-hours billed by Cost Explorer, giving {{COVERAGE}}% coverage and a {{RESIDUAL}} DPU-hour residual.">
        <defs>
          <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/>
          </marker>
          <marker id="ard" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="currentColor" opacity=".5"/>
          </marker>
        </defs>
        <g fill="none" stroke="currentColor" stroke-width="1.5">
          <rect x="20"  y="26"  width="200" height="58" rx="2"/>
          <rect x="268" y="8"   width="236" height="54" rx="2"/>
          <rect x="268" y="80"  width="236" height="66" rx="2"/>
          <rect x="20"  y="196" width="200" height="82" rx="2" stroke-dasharray="4 4" opacity=".55"/>
          <rect x="268" y="286" width="236" height="54" rx="2"/>
          <rect x="566" y="282" width="190" height="62" rx="2"/>
        </g>
        <rect x="566" y="34" width="190" height="62" rx="2" fill="none" stroke="var(--accent)" stroke-width="2"/>
        <rect x="808" y="130" width="172" height="122" rx="2" fill="none" stroke="var(--accent)" stroke-width="2"/>

        <g font-family="IBM Plex Mono, ui-monospace, monospace" font-size="12" fill="currentColor">
          <text x="36" y="48" font-weight="600">Glue GetJobRuns</text>
          <text x="36" y="66" font-size="11" opacity=".72">{{N_RUNS}} runs · {{N_JOBS_RUN}} jobs</text>

          <text x="284" y="30" font-weight="600">{{N_DPUS}} with DPUSeconds</text>
          <text x="284" y="48" font-size="11" opacity=".72">the figure AWS bills</text>

          <text x="284" y="104" font-weight="600">{{N_EST}} without DPUSeconds</text>
          <text x="284" y="121" font-size="11" opacity=".72">DPU × workers × duration,</text>
          <text x="284" y="136" font-size="11" opacity=".72">1-minute billing floor</text>

          <text x="36" y="222" font-weight="600" opacity=".6">CloudTrail StartJobRun</text>
          <text x="36" y="240" font-weight="600" opacity=".6">CW Logs · stream spans</text>
          <text x="36" y="262" font-size="11" opacity=".5">fallback inventory</text>

          <text x="284" y="308" font-weight="600">Cost Explorer</text>
          <text x="284" y="326" font-size="11" opacity=".72">{{USAGE_TYPE}} · ${{RATE}}/u</text>

          <text x="582" y="306" font-weight="600">Billed</text>
          <text x="582" y="328" font-size="15" font-weight="600">{{BILL}} DPU-h</text>
        </g>
        <g font-family="IBM Plex Mono, ui-monospace, monospace" fill="var(--accent)">
          <text x="582" y="58" font-size="12" font-weight="600">Attributed to jobs</text>
          <text x="582" y="82" font-size="15" font-weight="600">{{ATTR}} DPU-h</text>
          <text x="894" y="176" font-size="30" font-weight="600" text-anchor="middle">{{COVERAGE}}%</text>
          <text x="894" y="198" font-size="11" text-anchor="middle" fill="currentColor" opacity=".75">coverage</text>
          <text x="894" y="224" font-size="11" text-anchor="middle" fill="currentColor" opacity=".75">residual {{RESIDUAL}} DPU-h</text>
          <text x="894" y="240" font-size="11" text-anchor="middle" fill="currentColor" opacity=".75">{{RESID_SHORT}}</text>
        </g>

        <g stroke="currentColor" stroke-width="1.5" fill="none" marker-end="url(#ar)">
          <path d="M220,48 L268,35"/>
          <path d="M220,66 L268,110"/>
          <path d="M504,35 L566,56"/>
          <path d="M504,113 L566,80"/>
          <path d="M504,313 L566,313"/>
          <path d="M756,68 L808,166"/>
          <path d="M756,310 L808,218"/>
        </g>
        <path d="M220,232 C 400,232 430,150 566,104" stroke="currentColor" stroke-width="1.5" fill="none"
              stroke-dasharray="5 5" opacity=".5" marker-end="url(#ard)"/>
        <g font-family="IBM Plex Sans, sans-serif" font-size="10.5" fill="currentColor" opacity=".62">
          <text x="300" y="196">{{DELETED_NOTE}}</text>
          <text x="230" y="26">{{PCT_DPUS}}%</text>
          <text x="230" y="88">{{PCT_EST}}%</text>
          <text x="770" y="150" fill="var(--accent)" opacity="1" font-weight="600">÷</text>
        </g>
      </svg>
      <figcaption><b>{{PCT_DPUS}}% of the inventory is priced with the figure AWS bills, not with an estimate.</b>
      The {{N_EST}} runs without <code>DPUSeconds</code> are computed from the worker size in force for that run.
      The dashed path — rebuilding the inventory from CloudTrail and recovering durations from CloudWatch Logs
      stream spans — is what you need when jobs have been deleted, or deleted and recreated. {{RESID_VERDICT}}</figcaption>
    </figure>
  </div>


  <div class="grid2">
    <div class="card">
      <h3>How DPU is calculated</h3>
      <p class="cs">Where a run reports <code>DPUSeconds</code>, that value is used directly — it is what
      AWS bills. Otherwise it is <code>DPU(worker) × workers × duration</code>, using the worker
      configuration in force for that run and Glue’s 1-minute billing minimum.</p>
      <p class="cs">Worker sizing: G.1X = 1 DPU, G.2X = 2, G.4X = 4, G.8X = 8. Cost throughout assumes
      <strong>${{RATE}} per DPU-hour</strong>, the rate confirmed from this account’s own bill
      (cost ÷ usage quantity on <code>{{USAGE_TYPE}}</code>).</p>
      <p class="cs">The <b>DPU-h / run</b> column in the inventory averages each job’s last {{SAMPLE_RUNS}} billable
      runs — <code>SUCCEEDED</code>, <code>FAILED</code> or <code>TIMEOUT</code>. Failed runs bill, so they
      count.</p>
    </div>
    <div class="card">
      <h3>Reading the inventory</h3>
      <p class="cs">Click any row for that job’s full run history — every recorded run with its cost, the
      triggers that start it, its configuration, and the errors it has thrown. History reaches back as far
      as Glue retains job runs, to {{HIST_FROM}} on this account.</p>
      <p class="cs">The <b>Obs</b> dot marks jobs with <code>--enable-observability-metrics</code> on.
      Those feed CloudWatch’s <code>Glue</code> namespace, which is billed separately as CloudWatch custom metrics — a
      per-metric monthly charge that does not appear anywhere in the DPU-hour figures above.</p>
      <p class="cs">Runs marked <b>reconstructed</b> were recovered from CloudTrail because their job no
      longer exists. CloudTrail records that a run started and was billed, never how it ended, so their
      duration is a sampled median and their outcome is unknown.</p>
    </div>
  </div>

  <div class="card">
    <h3>What the bill cannot tell you</h3>
    <div class="find">{{FINDINGS}}</div>
  </div>
</details>


</div><!-- /listView -->

<div id="detailView" hidden>
  <button class="back" id="dBack">← All jobs</button>
  <header class="dh">
    <h1 id="dTitle"></h1>
    <div class="chips" id="dChips"></div>
    <p class="desc" id="dDesc" hidden></p>
  </header>
  <div class="kpi" id="dKpi"></div>

  <div class="card">
    <h3>Every run on record</h3>
    <p class="cs">One point per run, positioned by when it started and how many DPU-hours it billed.
    Steps in the line are resizes; red points are runs that were billed without producing anything.</p>
    <div class="legend2" id="dlg1"></div>
    <svg class="chart" id="dc1" viewBox="0 0 900 250" role="img"
      aria-label="Scatter of every recorded run for this job over time, by DPU-hours billed, with failed runs marked."></svg>
    <figcaption>History reaches back as far as Glue retains job runs — to {{HIST_FROM}} on this account.
    Anything older is gone from the API and would need the CloudTrail archive to recover.</figcaption>
  </div>

  <div class="card">
    <h3>Monthly spend</h3>
    <p class="cs">DPU-hours per month, split between runs that completed and runs that were billed but failed.</p>
    <div class="legend2" id="dlg2"></div>
    <svg class="chart" id="dc2" viewBox="0 0 900 220" role="img"
      aria-label="Stacked bars of DPU-hours per month for this job, split between completed and failed runs."></svg>
  </div>

  <div class="grid2">
    <div class="card">
      <h3>Configuration</h3>
      <p class="cs">As the job is defined today — a resize changes this without changing past runs.</p>
      <dl class="spec" id="dSpec"></dl>
    </div>
    <div class="card">
      <h3>What starts it</h3>
      <p class="cs">Glue triggers pointing at this job, and the triggers actually seen in its run history.</p>
      <div id="dTrig"></div>
      <div id="dTrigRuns"></div>
    </div>
  </div>

  <div class="card" id="dErrCard" hidden>
    <h3>Most common failures</h3>
    <p class="cs">Error messages from runs that were billed and produced nothing.</p>
    <div id="dErr"></div>
  </div>

  <div class="card">
    <h3>Run history</h3>
    <p class="cs" id="dRunCount"></p>
    <div class="tw">
      <table class="rt">
        <thead><tr>
          <th>Started (UTC)</th><th>Status</th><th class="num">Duration</th><th>Worker</th>
          <th class="num">DPU-h</th><th class="num">Cost</th><th>Trigger</th><th>Error</th>
        </tr></thead>
        <tbody id="dRuns"></tbody>
      </table>
    </div>
    <button class="more" id="dMore" hidden></button>
  </div>
</div>

</div>
<div id="tip"></div>

<script>
/* ---- theme toggle: light unless the reader has chosen otherwise ---- */
(function(){
  const root = document.documentElement;
  const btn = document.getElementById('themeBtn');
  const paint = t => {
    const next = t === 'dark' ? 'light' : 'dark';
    btn.setAttribute('aria-label', `Switch to ${next} theme`);
    btn.title = `Switch to ${next} theme`;
  };
  paint(root.getAttribute('data-theme') || 'light');
  btn.onclick = () => {
    const t = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', t);
    try { localStorage.setItem('glue-theme', t); } catch (e) {}
    paint(t);
  };
})();

const ROWS = {{ROWS_JSON}};

/* The environment convention is the account's, not ours: --env-prefixes sets it.
   The marker is looked for anywhere in the job name, not just at the front, because
   prod_etl_daily, etl_prod_daily and daily_etl_prod all mean the same thing. The
   separators around it are what bound the token, so reproduction_etl is not prod. */
const ENV_PREFIXES = {{ENV_PREFIXES_JSON}};
const NO_ENV = 'no env';
const ENV_MATCH = ENV_PREFIXES
  .map(p => p.replace(/[_\-.]+$/, ''))
  .filter(Boolean)
  .map(t => [t, new RegExp('(?:^|[_.-])' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') +
                           '(?:$|[_.-])', 'i')]);
const envOf = name => {
  for (const [token, re] of ENV_MATCH) if (re.test(name)) return token;
  return NO_ENV;
};
const ENV = r => envOf(r.job_name);
const STALE_DAYS = {{STALE_DAYS}};

// Glue versions come from the account, never from a list written here: an account may run
// 5.1 and no 3.0 at all, and a hardcoded strip would show an empty tile for a version it
// does not have while hiding the one most of its jobs are on.
const cmpVer = (a, b) => {
  const pa = String(a).split('.').map(Number), pb = String(b).split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++){
    const d = (pb[i] || 0) - (pa[i] || 0);
    if (d) return d;
  }
  return 0;
};
const ACT = r => r.last_run === 'NUNCA' ? 'never' : (r.days_ago > STALE_DAYS ? 'stale' : 'live');
ROWS.forEach(r => { r._env = ENV(r); r._act = ACT(r); r._dpu = r.avg_dpu_hours ?? -1;
                    r._obs = String(r.enable_obs_metrics).toLowerCase() === 'true'; });

const VERSIONS = [...new Set(ROWS.map(r => r.glue_version).filter(Boolean))].sort(cmpVer);
const verRank = v => VERSIONS.indexOf(v);
// VERSIONS is sorted newest first, so rank 0 is the best version this account runs
// and the last rank is the furthest behind. Two versions means one of each: with a
// higher version always preferable there is no honest reason to call the older one
// merely middling.
const verClass = v => {
  const i = verRank(v);
  if (i < 0) return 'vmid';
  if (i === 0) return 'vnew';
  return i === VERSIONS.length - 1 ? 'vold' : 'vmid';
};
const MAXDPU = Math.max(...ROWS.map(r => r._dpu));
const state = { q:'', v:new Set(), e:new Set(), a:new Set(), sort:'job_name', dir:1 };

const COLS = [
  {k:'job_name',   t:'Job',            cls:'job'},
  {k:'glue_version', t:'Glue',         cls:'ver'},
  {k:'worker',     t:'Worker',         cls:'worker'},
  {k:'last_run',   t:'Last run', cls:'when'},
  {k:'last_state', t:'Status',         cls:'st'},
  {k:'_dpu',       t:'DPU-h / run',    cls:'num', num:1},
  {k:'avg_exec_min', t:'Min / run',    cls:'num', num:1},
  {k:'avg_cost_run_usd', t:'USD / run',cls:'num', num:1},
  {k:'runs_sampled', t:'n',            cls:'num', num:1},
  {k:'_obs',       t:'Obs',            cls:'obs'},
];

function chips(host, key, opts){
  host.innerHTML = '';
  opts.forEach(o => {
    const b = document.createElement('button');
    b.className = 'chip'; b.type = 'button'; b.textContent = o;
    b.setAttribute('aria-pressed','false');
    b.onclick = () => {
      const s = state[key];
      s.has(o) ? s.delete(o) : s.add(o);
      b.setAttribute('aria-pressed', s.has(o) ? 'true':'false');
      render();
    };
    host.appendChild(b);
  });
}
chips(document.getElementById('fv'),'v',VERSIONS);
const ENV_VALUES = ENV_MATCH.map(([t]) => t).filter(t => ROWS.some(r => r._env === t))
  .concat(ROWS.some(r => r._env === NO_ENV) ? [NO_ENV] : []);
chips(document.getElementById('fe'),'e',ENV_VALUES);
chips(document.getElementById('fa'),'a',['live','stale','never']);
const ACTLBL = {live:'active', stale:'stale', never:'never'};
[...document.getElementById('fa').children].forEach(b => b.textContent = ACTLBL[b.textContent]);

document.getElementById('q').addEventListener('input', e => { state.q = e.target.value.toLowerCase(); render(); });

const hrow = document.getElementById('hrow');
COLS.forEach(c => {
  const th = document.createElement('th');
  th.textContent = c.t; th.dataset.k = c.k; th.tabIndex = 0;
  if (c.num) th.className = 'num';
  const go = () => {
    state.dir = state.sort === c.k ? -state.dir : (c.num ? -1 : 1);
    state.sort = c.k; render();
  };
  th.onclick = go;
  th.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } };
  hrow.appendChild(th);
});

function statePill(r){
  if (r.last_run === 'NUNCA') return '<span class="pill p-idle">never</span>';
  const m = {SUCCEEDED:['p-ok','ok'], FAILED:['p-crit','failed'], RUNNING:['p-warn','running'],
             TIMEOUT:['p-crit','timeout'], STOPPED:['p-idle','stopped']}[r.last_state] || ['p-idle', (r.last_state||'—').toLowerCase()];
  return `<span class="pill ${m[0]}">${m[1]}</span>`;
}

function render(){
  const f = ROWS.filter(r =>
    (!state.q || r.job_name.toLowerCase().includes(state.q)) &&
    (!state.v.size || state.v.has(r.glue_version)) &&
    (!state.e.size || state.e.has(r._env)) &&
    (!state.a.size || state.a.has(r._act)));

  const k = state.sort, d = state.dir;
  f.sort((a,b) => {
    let x = a[k], y = b[k];
    if (typeof x === 'boolean') { x = x?1:0; y = y?1:0; }
    if (x === null || x === undefined) x = -1;
    if (y === null || y === undefined) y = -1;
    if (typeof x === 'number' && typeof y === 'number') return (x-y)*d;
    return String(x).toLowerCase().localeCompare(String(y).toLowerCase()) * d;
  });

  hrow.querySelectorAll('th').forEach(th => {
    const base = COLS.find(c => c.k === th.dataset.k).t;
    th.innerHTML = th.dataset.k === k ? `${base}<span class="ar">${d>0?'▲':'▼'}</span>` : base;
  });

  document.getElementById('tb').innerHTML = f.map(r => {
    const pct = r._dpu > 0 ? Math.max(2, r._dpu / MAXDPU * 100) : 0;
    const bar = r._dpu > 0
      ? `<div class="dpu"><span>${r._dpu.toFixed(3)}</span><span class="track"><span class="fill" style="width:${pct}%"></span></span></div>`
      : '<div class="dpu"><span style="color:var(--ink-3)">—</span><span class="track"></span></div>';
    const ago = r.days_ago === null || r.days_ago === undefined ? ''
              : ` <span class="ago">${r.days_ago}d</span>`;
    const vcls = verClass(r.glue_version);
    return `<tr data-act="${r._act}" data-job="${r.job_name.replace(/"/g,'&quot;')}" tabindex="0">
      <td class="job">${r.job_name}<span class="arrow">→</span></td>
      <td><span class="pill ${vcls}">${r.glue_version}</span></td>
      <td class="worker">${r.worker}</td>
      <td class="when">${r.last_run === 'NUNCA' ? '<span style="color:var(--ink-3)">never</span>' : r.last_run + ago}</td>
      <td>${statePill(r)}</td>
      <td class="num">${bar}</td>
      <td class="num">${r.avg_exec_min ?? '—'}</td>
      <td class="num">${r.avg_cost_run_usd != null ? '$'+r.avg_cost_run_usd.toFixed(3) : '—'}</td>
      <td class="num" style="color:var(--ink-3)">${r.runs_sampled || '—'}</td>
      <td><span class="flagdot ${r._obs?'on':''}" title="${r._obs?'observability metrics ON':'off'}"></span></td>
    </tr>`;
  }).join('');

  document.getElementById('empty').hidden = f.length > 0;

  document.getElementById('tb').querySelectorAll('tr').forEach(tr => {
    const go = () => openJob(tr.dataset.job);
    tr.onclick = go;
    tr.onkeydown = ev => { if (ev.key === 'Enter'){ ev.preventDefault(); go(); } };
  });

  const dpuSum = f.reduce((s,r) => s + Math.max(r._dpu,0), 0);
  const costSum = f.reduce((s,r) => s + (r.avg_cost_run_usd || 0), 0);
  document.getElementById('count').textContent =
    `${f.length} of ${ROWS.length} jobs · ${dpuSum.toFixed(1)} DPU-h · $${costSum.toFixed(2)} for one full pass`;

  const verTiles = VERSIONS.map((v, i) => {
    const n = ROWS.filter(r => r.glue_version === v).length;
    const note = i === 0 ? 'newest in this account'
               : `${i} release${i > 1 ? 's' : ''} behind the newest here`;
    return [`Glue ${v}`, n, note, i > 0];
  });
  const noVer = ROWS.filter(r => !r.glue_version).length;
  if (noVer) verTiles.push(['No version set', noVer, 'python shell or legacy job', true]);
  const S = [
    ['Total jobs', ROWS.length, 'in the account', false],
    ...verTiles,
    ['Never run', ROWS.filter(r=>r._act==='never').length, 'not one execution', true],
    ['Stale', ROWS.filter(r=>r._act==='stale').length, `idle >${STALE_DAYS} days`, true],
    ['DPU-h per pass', ROWS.reduce((s,r)=>s+Math.max(r._dpu,0),0).toFixed(1), 'if every job ran once', false],
    ['Cost per pass', '$' + ROWS.reduce((s,r)=>s+(r.avg_cost_run_usd||0),0).toFixed(2), 'at $0.44 / DPU-h', false],
  ];
  document.getElementById('stats').innerHTML = S.map(([kk,v,n,hl]) =>
    `<div class="stat${hl?' hl':''}"><div class="k">${kk}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join('');
}
render();
</script>

<script>
/* ============ deep dive charts ============ */
const CH = {{CHARTS_JSON}};
const RATE = CH.totals.rate;
const SVGNS = 'http://www.w3.org/2000/svg';
const el = (n, a = {}) => { const e = document.createElementNS(SVGNS, n);
  for (const k in a) e.setAttribute(k, a[k]); return e; };
const fmt = (v, d = 1) => v.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
const usd = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
// "Sep 26" reads as a date, not September 2026. Show the month alone unless the
// window actually crosses a year boundary, where the year is doing real work.
const SPANS_YEARS = new Set((CH.compMonths || CH.months || [])
  .filter(k => /^\d{4}-/.test(k)).map(k => k.slice(0, 4))).size > 1;
const MLBL = k => k === '__earlier__' ? 'Earlier'
  : MON[+k.slice(5, 7) - 1] + (SPANS_YEARS ? ` ’${k.slice(2, 4)}` : '');

const tip = document.getElementById('tip');
function showTip(ev, html){
  tip.innerHTML = html; tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
const hideTip = () => { tip.style.opacity = 0; };
const row = (k, v) => `<div class="r"><span>${k}</span><b>${v}</b></div>`;

function legend(host, items){
  // kind: undefined = swatch, 'line' = rule, 'text' = the label itself carries the colour
  // (used where a colour marks type, not a series — a swatch there reads as a fourth bar).
  document.getElementById(host).innerHTML = items.map(([c, t, kind]) =>
    kind === 'text'
      ? `<span style="color:${c}">${t}</span>`
      : `<span><i class="${kind === 'line' || kind === 1 ? 'ln' : ''}" style="background:${c}"></i>${t}</span>`
  ).join('');
}

/* ---- recon stat tiles ---- */
(function(){
  const T = CH.totals;
  const S = [
    ['Billed', fmt(T.bill) + ' DPU-h', usd(T.bill * RATE) + ' at $' + RATE.toFixed(2) + '/unit', false],
    ['Attributed to jobs', fmt(T.attr) + ' DPU-h', T.runs.toLocaleString('en-US') + ' runs across ' + T.jobs + ' jobs', false],
    ['Coverage', T.bill ? (T.attr / T.bill * 100).toFixed(1) + '%' : '—', 'method target: ≥ 90%', true],
    ['Residual', fmt(T.bill - T.attr) + ' DPU-h', T.residNote, false],
  ];
  document.getElementById('recon').innerHTML = S.map(([k, v, n, hl]) =>
    `<div class="stat${hl ? ' hl' : ''}"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join('');
})();

/* ---- C1 · daily reconciliation ---- */
(function(){
  const s = document.getElementById('c1'), D = CH.daily;
  const W = 900, H = 280, L = 46, R = 14, T = 12, B = 36;
  const pw = W - L - R, ph = H - T - B, n = D.length;
  const max = Math.ceil(Math.max(...D.map(d => Math.max(d.bill, d.attr))) / 10) * 10;
  const X = i => L + i * pw / (n - 1);
  const Y = v => T + ph - v / max * ph;

  for (let t = 0; t <= 4; t++){
    const v = max * t / 4, y = Y(v);
    s.appendChild(el('line', {x1:L, x2:L+pw, y1:y, y2:y, class: t ? 'gl' : 'bl'}));
    const tx = el('text', {x:L-8, y:y+3.5, 'text-anchor':'end', class:'ax'});
    tx.textContent = v; s.appendChild(tx);
  }
  const yl = el('text', {x:L-8, y:T-2, 'text-anchor':'end', class:'axl'});
  yl.textContent = 'DPU-h'; s.appendChild(yl);

  D.forEach((d, i) => { if (d.d.slice(8) === '01'){
    s.appendChild(el('line', {x1:X(i), x2:X(i), y1:T, y2:T+ph, class:'gl'}));
    const t = el('text', {x:X(i)+4, y:H-16, class:'ax'}); t.textContent = MLBL(d.d.slice(0,7)); s.appendChild(t);
  }});

  if (CH.multi){
    // Stack by attribution source, then the unattributed remainder on top: the picture
    // shows the finding and the method's honesty in one read.
    const LAYERS = [['api','var(--s1)'], ['ct','var(--s2)'], ['s3','var(--s4)'],
                    ['__resid__','var(--sResid)']];
    const val = (d,k) => k === '__resid__' ? Math.max(d.bill - d.attr, 0) : (d[k] || 0);
    const acc = D.map(() => 0);
    LAYERS.forEach(([k,c]) => {
      const lower = acc.slice();
      D.forEach((d,i) => { acc[i] += val(d,k); });
      const up = D.map((d,i) => `${X(i)},${Y(acc[i])}`).join(' L');
      const down = D.map((d,i) => `${X(n-1-i)},${Y(lower[n-1-i])}`).join(' L');
      s.appendChild(el('path', {d:`M${up} L${down} Z`, fill:c, 'fill-opacity':'.85'}));
    });
    s.appendChild(el('path', {d:'M' + D.map((d,i) => `${X(i)},${Y(d.bill)}`).join(' L'),
      fill:'none', stroke:'var(--ink)', 'stroke-width':1.5, 'stroke-opacity':'.55'}));
  } else {
    const area = `M${X(0)},${Y(0)} ` + D.map((d,i) => `L${X(i)},${Y(d.attr)}`).join(' ') + ` L${X(n-1)},${Y(0)} Z`;
    s.appendChild(el('path', {d:area, fill:'var(--s1)', 'fill-opacity':'.22'}));
    s.appendChild(el('path', {d:'M' + D.map((d,i) => `${X(i)},${Y(d.attr)}`).join(' L'),
      fill:'none', stroke:'var(--s1)', 'stroke-width':2, 'stroke-linejoin':'round'}));
    s.appendChild(el('path', {d:'M' + D.map((d,i) => `${X(i)},${Y(d.bill)}`).join(' L'),
      fill:'none', stroke:'var(--s2)', 'stroke-width':2, 'stroke-linejoin':'round'}));
  }

  (CH.anoms || []).forEach(({day, lab}, k) => {
    const i = D.findIndex(d => d.d === day); if (i < 0) return;
    s.appendChild(el('line', {x1:X(i), x2:X(i), y1:T, y2:T+ph, class:'annoline'}));
    const t = el('text', {x:X(i) + (k ? 6 : -6), y:T + 14 + k * 15,
      'text-anchor': k ? 'start' : 'end', class:'anno'});
    t.textContent = lab; s.appendChild(t);
  });

  const cross = el('line', {x1:0, x2:0, y1:T, y2:T+ph, stroke:'var(--ink-3)', 'stroke-width':1, opacity:0});
  s.appendChild(cross);
  const hit = el('rect', {x:L, y:T, width:pw, height:ph, fill:'transparent'});
  s.appendChild(hit);
  hit.addEventListener('mousemove', ev => {
    const bb = s.getBoundingClientRect();
    const i = Math.max(0, Math.min(n-1, Math.round(((ev.clientX - bb.left) / bb.width * W - L) / pw * (n-1))));
    const d = D[i];
    cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('opacity', .6);
    showTip(ev, `<div class="t">${d.d}</div>` +
      row('cost', usd(d.bill * RATE)) +
      row('billed', fmt(d.bill,1) + ' DPU-h') + row('attributed', fmt(d.attr,1) + ' DPU-h') +
      (CH.multi ? row('· Glue API', fmt(d.api,1)) +
        (d.ct ? row('· CloudTrail history', fmt(d.ct,1)) : '') +
        (d.s3 ? row('· CloudTrail archive', fmt(d.s3,1)) : '') : '') +
      row('coverage', d.bill > 0.5 ? (d.attr/d.bill*100).toFixed(1) + '%' : '—'));
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  legend('lg1', CH.multi
    ? [['var(--s1)','Glue API run history'], ['var(--s2)','CloudTrail Event history + log spans'],
       ['var(--s4)','CloudTrail S3 archive + log spans'], ['var(--sResid)','Residual — unattributed'],
       ['var(--ink)','Billed by AWS (line)', 'line']]
    : [['var(--s1)','Attributed to jobs (area)'], ['var(--s2)','Billed by AWS (line)', 'line']]);
})();

/* ---- C2 · monthly composition ---- */
(function(){
  const s = document.getElementById('c2'), C = CH.comp, top6 = CH.top6;
  const KEYS = [...top6, '__other__', '__resid__'];
  const COL = ['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--sOther)','var(--sResid)'];
  const NAME = k => k === '__other__' ? ('Everything else (' + CH.totals.otherJobs + ' jobs)') : k === '__resid__' ? 'Unattributed residual' : k;
  const W = 440, H = 300, L = 42, R = 10, T = 12, B = 34;
  const pw = W-L-R, ph = H-T-B;
  const max = Math.ceil(Math.max(...C.map(c => c.bill)) / 200) * 200;
  const Y = v => T + ph - v / max * ph;
  const bw = 74, step = pw / C.length;

  for (let t = 0; t <= 5; t++){
    const v = max*t/5, y = Y(v);
    s.appendChild(el('line', {x1:L, x2:L+pw, y1:y, y2:y, class: t ? 'gl':'bl'}));
    const tx = el('text', {x:L-7, y:y+3.5, 'text-anchor':'end', class:'ax'}); tx.textContent = v; s.appendChild(tx);
  }
  const yl = el('text', {x:L-7, y:T-2, 'text-anchor':'end', class:'axl'}); yl.textContent='DPU-h'; s.appendChild(yl);

  C.forEach((c, ci) => {
    const cx = L + step*ci + step/2, x = cx - bw/2;
    let acc = 0;
    KEYS.forEach((k, ki) => {
      const v = c[k] || 0; if (v <= 0) return;
      const y0 = Y(acc), y1 = Y(acc+v), h = Math.max(y0-y1-2, 1);
      const r = el('rect', {x, y:y1, width:bw, height:h, fill:COL[ki], rx:1});
      r.addEventListener('mousemove', ev => showTip(ev,
        `<div class="t">${NAME(k)}</div>` + row(MLBL(c.m), fmt(v,1)+' DPU-h') +
        row('cost', usd(v*RATE)) + row('of the month', (v/c.bill*100).toFixed(1)+'%')));
      r.addEventListener('mouseleave', hideTip);
      s.appendChild(r); acc += v;
    });
    const t = el('text', {x:cx, y:H-16, 'text-anchor':'middle', class:'axl'}); t.textContent = MLBL(c.m); s.appendChild(t);
    const tv = el('text', {x:cx, y:Y(c.bill)-7, 'text-anchor':'middle', class:'vlab'});
    tv.textContent = fmt(c.bill,0); s.appendChild(tv);
  });
  legend('lg2', KEYS.map((k,i) => [COL[i], NAME(k)]));
})();

/* ---- C3 · top 15 ---- */
(function(){
  const s = document.getElementById('c3'), D = CH.top15, M = CH.months;
  // Take as many steps as there are months, from the prominent end: a 4-month window
  // then never touches the dimmest step, and no month falls off the end of the ramp
  // into an undefined fill.
  const SLOTS = 5;
  const first = Math.max(1, SLOTS - M.length + 1);
  const COL = M.map((_, i) => `var(--o${Math.min(SLOTS, first + i)})`);
  const W = 900, H = 520, L = 236, R = 104, T = 22, B = 30;
  const pw = W-L-R, ph = H-T-B;
  const max = Math.ceil(Math.max(...D.map(d => d.total)) / 50) * 50;
  const X = v => v / max * pw, rowH = ph / D.length, bh = Math.min(rowH - 11, 20);

  for (let t = 0; t <= 4; t++){
    const v = max*t/4, x = L + X(v);
    s.appendChild(el('line', {x1:x, x2:x, y1:T-6, y2:T+ph, class: t ? 'gl':'bl'}));
    const tx = el('text', {x, y:H-14, 'text-anchor':'middle', class:'ax'}); tx.textContent = v; s.appendChild(tx);
  }
  const xl = el('text', {x:L, y:T-10, class:'axl'}); xl.textContent='DPU-h'; s.appendChild(xl);

  D.forEach((d, i) => {
    const y = T + rowH*i + (rowH-bh)/2;
    const nm = el('text', {x:L-10, y:y+bh/2+4, 'text-anchor':'end', class:'jlab'});
    nm.textContent = d.job.length > 32 ? d.job.slice(0,31)+'…' : d.job;
    nm.setAttribute('style','cursor:pointer');
    nm.addEventListener('click', () => openJob(d.job));
    if (envOf(d.job) === 'no prefix') nm.setAttribute('fill','var(--sResid)');
    s.appendChild(nm);
    let acc = 0;
    M.forEach((m, mi) => {
      const v = d[m] || 0; if (v <= 0) return;
      const x0 = L + X(acc), w = Math.max(X(v) - 2, 1);
      const r = el('rect', {x:x0, y, width:w, height:bh, fill:COL[mi], rx:1, style:'cursor:pointer'});
      r.addEventListener('click', () => openJob(d.job));
      r.addEventListener('mousemove', ev => showTip(ev,
        `<div class="t">${d.job}</div>` + row(MLBL(m), fmt(v,1)+' DPU-h') + row('cost that month', usd(v*RATE)) +
        row('total Jun–Aug', fmt(d.total,1)+' DPU-h') + row('total cost', usd(d.total*RATE)) +
        row('runs', d.runs.toLocaleString('en-US'))));
      r.addEventListener('mouseleave', hideTip);
      s.appendChild(r); acc += v;
    });
    const tv = el('text', {x:L+X(d.total)+8, y:y+bh/2+4, class:'vlab'});
    tv.textContent = fmt(d.total,1) + '  ' + usd(d.total*RATE); s.appendChild(tv);
  });
  legend('lg3', M.map((m,i) => [COL[i], MLBL(m)]).concat([['var(--sResid)','name in red = no environment marker', 'text']]));
})();

/* ---- C4 · duration ---- */
(function(){
  const s = document.getElementById('c4'), D = CH.dur;
  const tR = D.reduce((a,b) => a+b.runs, 0), tD = D.reduce((a,b) => a+b.dpuh, 0);
  const W = 440, H = 300, L = 38, R = 8, T = 18, B = 48;
  const pw = W-L-R, ph = H-T-B;
  // Scale to the data: a fixed ceiling sends any band above it straight out of the plot.
  const peak = Math.max(...D.map(d => Math.max(d.runs/tR*100, d.dpuh/tD*100)), 1);
  const max = Math.max(20, Math.ceil(peak / 10) * 10);
  const Y = v => T + ph - v/max*ph;
  const step = pw / D.length, bw = Math.min((step-14)/2, 20);

  for (let t = 0; t <= 4; t++){
    const v = max*t/4, y = Y(v);
    s.appendChild(el('line', {x1:L, x2:L+pw, y1:y, y2:y, class: t ? 'gl':'bl'}));
    const tx = el('text', {x:L-7, y:y+3.5, 'text-anchor':'end', class:'ax'});
    tx.textContent = v.toFixed(0)+'%'; s.appendChild(tx);
  }
  D.forEach((d, i) => {
    const cx = L + step*i + step/2;
    [[d.runs/tR*100, 'var(--s1)', -1, 'runs', d.runs.toLocaleString('en-US')],
     [d.dpuh/tD*100, 'var(--s2)',  1, 'DPU-h', fmt(d.dpuh,1)]].forEach(([p, c, side, lab, raw]) => {
      const x = cx + (side < 0 ? -bw-1 : 1), h = Math.max(p/max*ph, 1);
      const r = el('rect', {x, y:Y(p), width:bw, height:h, fill:c, rx:1});
      r.addEventListener('mousemove', ev => showTip(ev,
        `<div class="t">${d.label}</div>` + row(lab, raw) + row('of total', p.toFixed(1)+'%') +
        (lab === 'DPU-h' ? row('cost', usd(d.dpuh*RATE)) : '')));
      r.addEventListener('mouseleave', hideTip);
      s.appendChild(r);
      const t = el('text', {x:x+bw/2, y:Y(p)-5, 'text-anchor':'middle', class:'vlab', 'font-size':'9.5'});
      t.textContent = p.toFixed(0); s.appendChild(t);
    });
    const lb = el('text', {x:cx, y:H-28, 'text-anchor':'middle', class:'axl'});
    lb.textContent = d.label; s.appendChild(lb);
  });
  const nt = el('text', {x:L, y:H-8, class:'ax'});
  nt.textContent = CH.totals.durNote; s.appendChild(nt);
  legend('lg4', [['var(--s1)','% of runs'], ['var(--s2)','% of DPU-hours']]);
})();

</script>

<script>
/* ============ per-job detail view ============ */
const DET = {{DETAIL_JSON}};
const BASE = new Date(DET.base).getTime();
const DPUW = {'G.1X':1,'G.2X':2,'G.4X':4,'G.8X':8,'G.025X':.25,'Standard':1,'Z.2X':2};
const BAD = new Set(['FAILED','TIMEOUT','STOPPED','ERROR']);
const R_T=0, R_E=1, R_D=2, R_S=3, R_W=4, R_N=5, R_TR=6, R_ER=7;

const runDate = r => new Date(BASE + r[R_T]*60000);
const runState = r => DET.states[r[R_S]] || 'UNKNOWN';
const runWorker = r => r[R_W] >= 0 ? `${DET.workers[r[R_W]]} ×${r[R_N]}` : '—';
const runDPU = r => r[R_W] >= 0 ? (DPUW[DET.workers[r[R_W]]] || 1) * (r[R_N] || 0) : null;
function runHours(r){
  if (r[R_D] >= 0) return r[R_D] / 3600;
  const d = runDPU(r); return d ? d * Math.max(r[R_E], 60) / 3600 : 0;
}
const iso = d => d.toISOString().slice(0,16).replace('T',' ');
const dur = s => s >= 3600 ? (s/3600).toFixed(2)+' h' : s >= 60 ? (s/60).toFixed(1)+' min' : s+' s';
const q = (a, p) => a.length ? a.slice().sort((x,y)=>x-y)[Math.min(a.length-1, Math.floor(a.length*p))] : 0;

const listView = document.getElementById('listView');
const detailView = document.getElementById('detailView');

function openJob(name){ location.hash = '#job/' + encodeURIComponent(name); }

function route(){
  const m = location.hash.match(/^#job\/(.+)$/);
  if (m){
    const name = decodeURIComponent(m[1]);
    if (DET.meta[name]){ renderDetail(name); listView.hidden = true; detailView.hidden = false;
      window.scrollTo(0,0); return; }
  }
  detailView.hidden = true; listView.hidden = false;
}
addEventListener('hashchange', route);

function renderDetail(name){
  const M = DET.meta[name], R = DET.runs[name] || [];
  const billable = R.filter(r => !['RUNNING','WAITING'].includes(runState(r)));
  const hrs = billable.map(runHours);
  const total = hrs.reduce((a,b)=>a+b, 0);
  const okR = billable.filter(r => runState(r) === 'SUCCEEDED');
  const badR = billable.filter(r => BAD.has(runState(r)));
  const wasted = badR.reduce((a,r)=>a+runHours(r), 0);
  const secs = billable.filter(r => r[R_E] > 0).map(r => r[R_E]);
  const env = envOf(name);
  const first = R.length ? runDate(R[0]) : null, last = R.length ? runDate(R[R.length-1]) : null;
  const days = first && last ? Math.max(1, (last-first)/864e5) : 1;

  const vcls = verClass(M.gv);
  document.getElementById('dTitle').textContent = name;
  document.getElementById('dChips').innerHTML =
    `<span class="pill ${vcls}">Glue ${M.gv}</span>` +
    `<span class="pill p-idle">${env}</span>` +
    `<span class="pill p-idle">${M.wt ? M.wt + ' ×' + M.nw : M.cmd}</span>` +
    (M.flags['--enable-observability-metrics'] === 'true'
      ? '<span class="pill p-warn">observability metrics on</span>' : '') +
    (badR.length && badR.length / Math.max(billable.length,1) > .2
      ? `<span class="pill p-crit">${Math.round(badR.length/billable.length*100)}% failure rate</span>` : '');
  document.getElementById('dDesc').textContent = M.desc || '';
  document.getElementById('dDesc').hidden = !M.desc;

  const K = [
    ['Runs on record', R.length.toLocaleString('en-US'), first ? iso(first).slice(0,10) + ' →' : 'never run'],
    ['DPU-hours', fmt(total, 1), 'billable runs only'],
    ['Total cost', usd(total * RATE), 'at $' + RATE.toFixed(2) + ' / DPU-hour'],
    ['Cost per run', billable.length ? usd(total / billable.length * RATE) : '—', 'average'],
    ['Success rate', billable.length ? (okR.length / billable.length * 100).toFixed(0) + '%' : '—',
      `${badR.length} failed / stopped`],
    ['Median duration', secs.length ? dur(q(secs, .5)) : '—', secs.length ? 'p95 ' + dur(q(secs, .95)) : ''],
    ['Wasted on failures', usd(wasted * RATE), fmt(wasted,1) + ' DPU-h'],
    ['Spend rate', usd(total * RATE / days * 30), 'per 30 days observed'],
  ];
  document.getElementById('dKpi').innerHTML = K.map(([k,v,n]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join('');

  /* ---- spec ---- */
  const flag = k => M.flags[k] === undefined
    ? '<span class="off">not set</span>'
    : (M.flags[k] === '' ? '<em>present (enabled)</em>' : M.flags[k]);
  const S = [
    ['Worker', M.wt ? `${M.wt} × ${M.nw}  (${(DPUW[M.wt]||1)*M.nw} DPU)` : M.cmd],
    ['Runtime', `Glue ${M.gv} · ${M.cmd} · python ${M.py || '—'}`],
    ['Timeout', M.to ? M.to + ' min' : '<span class="off">default</span>'],
    ['Max retries', String(M.retry)],
    ['Max concurrent runs', String(M.conc)],
    ['IAM role', M.role || '—'],
    ['Connections', M.conn.length ? M.conn.join(', ') : '<span class="off">none</span>'],
    ['Script', M.script || '—'],
    ['Job arguments', M.nargs + ' set'],
    ['--enable-observability-metrics', flag('--enable-observability-metrics')],
    ['--enable-metrics', flag('--enable-metrics')],
    ['--enable-spark-ui', flag('--enable-spark-ui')],
    ['--enable-auto-scaling', flag('--enable-auto-scaling')],
    ['--job-bookmark-option', flag('--job-bookmark-option')],
    ['--datalake-formats', flag('--datalake-formats')],
    ['Created / modified', `${M.created}  ·  ${M.modified}`],
  ];
  document.getElementById('dSpec').innerHTML =
    S.map(([k,v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

  /* ---- triggers ---- */
  const T = DET.trigmap[name] || [];
  document.getElementById('dTrig').innerHTML = T.length
    ? T.map(t => `<div class="trg"><span class="nm">${t.n}</span>` +
        `<span class="mt">${t.ty}${t.sch ? ' · ' + t.sch : ''}</span>` +
        `<span class="pill ${t.st === 'ACTIVATED' ? 'p-ok' : 'p-idle'}">${(t.st||'').toLowerCase()}</span>` +
        (t.wf ? `<span class="mt">workflow ${t.wf}</span>` : '') + '</div>').join('')
    : '<p class="cs" style="margin:0">No Glue trigger starts this job. It runs from a Step Function, ' +
      'an external scheduler, or by hand.</p>';
  const runTrigs = [...new Set(R.map(r => r[R_TR] >= 0 ? DET.trigs[r[R_TR]] : null).filter(Boolean))];
  document.getElementById('dTrigRuns').innerHTML = runTrigs.length
    ? '<div class="trg" style="border-top:1px solid var(--rule);margin-top:6px">' +
      `<span class="mt">observed starting this job: ${runTrigs.map(t=>`<b>${t}</b>`).join(', ')}</span></div>` : '';

  /* ---- errors ---- */
  const errs = {};
  badR.forEach(r => { if (r[R_ER] >= 0){ const e = DET.errs[r[R_ER]]; errs[e] = (errs[e]||0)+1; } });
  const eList = Object.entries(errs).sort((a,b)=>b[1]-a[1]).slice(0,6);
  document.getElementById('dErrCard').hidden = !eList.length;
  document.getElementById('dErr').innerHTML = eList.map(([e,n]) =>
    `<div class="errrow"><span class="n">${n}×</span><span class="m">${e.replace(/[<>]/g,'')}</span></div>`).join('');

  drawRunSeries(R);
  drawMonthly(billable);
  renderRunTable(R);
}

/* ---- chart: every run over time ---- */
function drawRunSeries(R){
  const s = document.getElementById('dc1'); s.innerHTML = '';
  const W = 900, H = 250, L = 46, Rm = 14, T = 14, B = 34;
  const pw = W-L-Rm, ph = H-T-B;
  if (!R.length){
    const t = el('text', {x:W/2, y:H/2, 'text-anchor':'middle', class:'axl'});
    t.textContent = 'This job has no runs on record.'; s.appendChild(t); return;
  }
  const t0 = runDate(R[0]).getTime(), t1 = runDate(R[R.length-1]).getTime();
  const span = Math.max(t1 - t0, 864e5);
  const max = Math.max(...R.map(runHours)) * 1.12 || 1;
  const X = t => L + (t - t0) / span * pw, Y = v => T + ph - v/max*ph;

  for (let i = 0; i <= 4; i++){
    const v = max*i/4, y = Y(v);
    s.appendChild(el('line', {x1:L, x2:L+pw, y1:y, y2:y, class: i ? 'gl':'bl'}));
    const tx = el('text', {x:L-8, y:y+3.5, 'text-anchor':'end', class:'ax'});
    tx.textContent = v.toFixed(v < 1 ? 2 : 1); s.appendChild(tx);
  }
  const yl = el('text', {x:L-8, y:T-2, 'text-anchor':'end', class:'axl'});
  yl.textContent = 'DPU-h'; s.appendChild(yl);
  const months = new Set();
  R.forEach(r => { const d = runDate(r), k = d.toISOString().slice(0,7);
    if (!months.has(k)){ months.add(k);
      const x = X(new Date(k + '-01T00:00Z').getTime());
      if (x >= L && x <= L+pw){
        s.appendChild(el('line', {x1:x, x2:x, y1:T, y2:T+ph, class:'gl'}));
        const t = el('text', {x:x+4, y:H-14, class:'ax'}); t.textContent = k.slice(2); s.appendChild(t);
      }}});
  s.appendChild(el('path', {d:'M' + R.map(r => `${X(runDate(r).getTime())},${Y(runHours(r))}`).join(' L'),
    fill:'none', stroke:'var(--s1)', 'stroke-width':1, opacity:.35}));
  R.forEach(r => {
    const bad = BAD.has(runState(r)), h = runHours(r);
    const c = el('circle', {cx:X(runDate(r).getTime()), cy:Y(h), r: bad ? 4 : 3,
      fill: bad ? 'var(--sResid)' : 'var(--s1)', stroke:'var(--surface)', 'stroke-width':1.5});
    c.addEventListener('mousemove', ev => showTip(ev,
      `<div class="t">${iso(runDate(r))}</div>` +
      row('status', runState(r)) + row('duration', dur(r[R_E])) +
      row('worker', runWorker(r)) + row('DPU-h', fmt(h, 3)) + row('cost', usd(h*RATE)) +
      (r[R_TR] >= 0 ? row('trigger', DET.trigs[r[R_TR]]) : '')));
    c.addEventListener('mouseleave', hideTip);
    s.appendChild(c);
  });
  legend('dlg1', [['var(--s1)','completed run'], ['var(--sResid)','failed · timeout · stopped']]);
}

/* ---- chart: monthly ---- */
function drawMonthly(R){
  const s = document.getElementById('dc2'); s.innerHTML = '';
  const by = {};
  R.forEach(r => { const k = runDate(r).toISOString().slice(0,7);
    (by[k] = by[k] || {ok:0, bad:0, n:0}); by[k].n++;
    by[k][BAD.has(runState(r)) ? 'bad' : 'ok'] += runHours(r); });
  const K = Object.keys(by).sort();
  const W = 900, H = 220, L = 46, Rm = 14, T = 14, B = 34;
  const pw = W-L-Rm, ph = H-T-B;
  if (!K.length) return;
  const max = Math.max(...K.map(k => by[k].ok + by[k].bad)) * 1.1 || 1;
  const Y = v => T + ph - v/max*ph, step = pw/K.length, bw = Math.min(step-10, 46);
  for (let i = 0; i <= 4; i++){
    const v = max*i/4, y = Y(v);
    s.appendChild(el('line', {x1:L, x2:L+pw, y1:y, y2:y, class: i ? 'gl':'bl'}));
    const tx = el('text', {x:L-8, y:y+3.5, 'text-anchor':'end', class:'ax'});
    tx.textContent = v.toFixed(v < 10 ? 1 : 0); s.appendChild(tx);
  }
  const yl = el('text', {x:L-8, y:T-2, 'text-anchor':'end', class:'axl'});
  yl.textContent = 'DPU-h'; s.appendChild(yl);
  K.forEach((k, i) => {
    const cx = L + step*i + step/2, x = cx - bw/2, d = by[k];
    let acc = 0;
    [['ok','var(--s1)','completed'], ['bad','var(--sResid)','failed']].forEach(([f, c, lab]) => {
      const v = d[f]; if (v <= 0) return;
      const y0 = Y(acc), y1 = Y(acc+v);
      const r = el('rect', {x, y:y1, width:bw, height:Math.max(y0-y1-2,1), fill:c, rx:1});
      r.addEventListener('mousemove', ev => showTip(ev, `<div class="t">${k}</div>` +
        row(lab, fmt(v,2)+' DPU-h') + row('cost', usd(v*RATE)) + row('runs in month', d.n)));
      r.addEventListener('mouseleave', hideTip);
      s.appendChild(r); acc += v;
    });
    const t = el('text', {x:cx, y:H-14, 'text-anchor':'middle', class:'ax'});
    t.textContent = k.slice(2); s.appendChild(t);
    const tv = el('text', {x:cx, y:Y(acc)-6, 'text-anchor':'middle', class:'vlab', 'font-size':'9.5'});
    tv.textContent = usd(acc*RATE); s.appendChild(tv);
  });
  legend('dlg2', [['var(--s1)','completed'], ['var(--sResid)','failed · timeout · stopped']]);
}

/* ---- run table ---- */
let shown = 50, curRuns = [];
function renderRunTable(R){
  curRuns = R.slice().reverse(); shown = 50; paintRuns();
}
function paintRuns(){
  const slice = curRuns.slice(0, shown);
  document.getElementById('dRuns').innerHTML = slice.map(r => {
    const st = runState(r), bad = BAD.has(st), h = runHours(r);
    const cls = st === 'SUCCEEDED' ? 'p-ok' : bad ? 'p-crit' : st === 'ESTIMATED' ? 'p-warn' : 'p-idle';
    return `<tr><td class="when">${iso(runDate(r))}</td>` +
      `<td><span class="pill ${cls}">${st === 'ESTIMATED' ? 'reconstructed' : st.toLowerCase()}</span></td>` +
      `<td class="num">${dur(r[R_E])}</td>` +
      `<td class="worker">${runWorker(r)}</td>` +
      `<td class="num">${fmt(h,3)}</td>` +
      `<td class="num">${usd(h*RATE)}</td>` +
      `<td class="worker">${r[R_TR] >= 0 ? DET.trigs[r[R_TR]] : '<span style="color:var(--ink-3)">manual</span>'}</td>` +
      `<td class="err">${r[R_ER] >= 0 ? DET.errs[r[R_ER]].replace(/[<>]/g,'').slice(0,160) : ''}</td></tr>`;
  }).join('');
  const b = document.getElementById('dMore');
  b.hidden = shown >= curRuns.length;
  b.textContent = `show ${Math.min(200, curRuns.length - shown)} more (${curRuns.length - shown} left)`;
  document.getElementById('dRunCount').textContent =
    `${Math.min(shown, curRuns.length)} of ${curRuns.length} runs · newest first`;
}
document.getElementById('dMore').onclick = () => { shown += 200; paintRuns(); };
document.getElementById('dBack').onclick = () => { location.hash = ''; };
route();

</script>
</body>
</html>
"""


# ------------------------------------------------------------------------- render

def build_charts(data: dict, notes: dict, other_jobs: int) -> dict:
    rate = data["rate"]
    daily = []
    for day, qty in sorted(data["bill_day"].items()):
        buckets = data["by_day_src"].get(day, {})
        daily.append({"d": day, "bill": round(qty, 3),
                      "attr": round(data["by_day"].get(day, 0.0), 3),
                      "api": round(buckets.get("api", 0.0), 3),
                      "ct": round(buckets.get("ct", 0.0), 3),
                      "s3": round(buckets.get("s3", 0.0), 3)})
    multi = any(d["ct"] or d["s3"] for d in daily)
    # The blue ramp carries five distinguishable ordinal steps and no more. A window wider
    # than that folds its oldest months into one segment rather than inventing a sixth step
    # or letting a month drop off the end of the ramp.
    MAX_SEG = 5
    months = data["months"]
    if len(months) > MAX_SEG:
        keep = months[-(MAX_SEG - 1):]
        older = [m for m in months if m not in keep]
        segments = ["__earlier__"] + keep
    else:
        keep, older, segments = months, [], months
    top15 = []
    for job in data["top15"]:
        row = {"job": job, "total": round(data["by_job"][job], 2),
               "runs": data["job_runs"][job]}
        if older:
            row["__earlier__"] = round(
                sum(data["job_month"].get((job, m), 0.0) for m in older), 2)
        for m in keep:
            row[m] = round(data["job_month"].get((job, m), 0.0), 2)
        top15.append(row)
    anoms = []
    for i, (day, _, _) in enumerate(data["anomalies"]):
        anoms.append({"day": day, "lab": ("anomalous day" if i else "check these two days")})
    resid = data["bill_qty"] - data["attributed"]
    days = max(len(data["bill_day"]), 1)
    return {
        "daily": daily, "comp": data["comp"], "top15": top15, "dur": data["duration"],
        "top6": data["top6"], "months": segments, "compMonths": data["months"],
        "anoms": anoms, "multi": multi,
        "totals": {"bill": round(data["bill_qty"], 1), "attr": round(data["attributed"], 1),
                   "runs": len(data["window"]),
                   "jobs": len({r["job"] for r in data["window"]}),
                   "rate": rate, "otherJobs": other_jobs,
                   "durNote": notes["DUR_AXIS_NOTE"],
                   "residNote": f"{resid / days:.2f} DPU-h/day"},
    }


def render(template: str, text: dict[str, str], blobs: dict[str, str]) -> str:
    """Text tokens first so the completeness check runs before the JSON goes in —
    a job name containing braces must not read as an unfilled token."""
    out = template
    for key, value in text.items():
        out = out.replace("{{" + key + "}}", str(value))
    leftover = sorted(set(re.findall(r"\{\{(\w+)\}\}", out)) - set(blobs))
    if leftover:
        raise AwsError(f"template tokens left unfilled: {', '.join(leftover)}")
    for key, value in blobs.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def default_window(today: dt.date) -> tuple[str, str]:
    """The last 90 complete days. Today is excluded — Cost Explorer has not finished
    posting it. This also matches CloudTrail Event history's own 90-day lookback, so
    --reconstruct can reach every day in the window without the S3 trail archive."""
    # Use the same expression the reconstruction uses for its lookback, or the window
    # starts one day before Event history can reach and that day is silently API-only.
    return (today - dt.timedelta(days=LOOKBACK_DAYS - 1)).isoformat(), today.isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="AWS CLI profile")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--start", help=f"YYYY-MM-DD (default: {LOOKBACK_DAYS} days ago)")
    ap.add_argument("--end", help="YYYY-MM-DD, exclusive (default: today)")
    ap.add_argument("--out", help="output path (default: glue-cost-<profile>.html)")
    ap.add_argument("--env-prefixes", default=",".join(DEFAULT_ENV_PREFIXES),
                    help="comma-separated markers that name an environment, matched "
                         "anywhere in the job name rather than only at the front "
                         f"(default: {','.join(DEFAULT_ENV_PREFIXES)}). Pass an empty "
                         "string to disable environment grouping entirely.")
    ap.add_argument("--reconstruct", action="store_true",
                    help="recover runs the Glue API lost to deleted jobs, from CloudTrail "
                         "Event history plus CloudWatch Logs stream spans (slow)")
    ap.add_argument("--archive-bucket",
                    help="CloudTrail S3 trail bucket, to reach past the 90-day Event history "
                         "window (aws cloudtrail describe-trails). Implies --reconstruct")
    args = ap.parse_args()

    today = dt.datetime.now(dt.timezone.utc)
    start, end = default_window(today.date())
    start, end = args.start or start, args.end or end
    say = lambda msg: print(msg, file=sys.stderr)

    say(f"· Cost Explorer: AWS Glue, {start} → {end}")
    cost = fetch_cost(args.profile, args.region, start, end)
    say("· Glue jobs")
    jobs = fetch_jobs(args.profile, args.region)
    if not jobs:
        say("No Glue jobs in this account/region. Nothing to attribute.")
        return 1
    say(f"· Run history for {len(jobs)} jobs")
    runs = fetch_runs(args.profile, args.region, [j["Name"] for j in jobs])
    say("· Triggers")
    triggers = fetch_triggers(args.profile, args.region)

    acct = account_id(args.profile, args.region)
    if args.reconstruct or args.archive_bucket:
        extra = reconstruct(args.profile, args.region, runs, start, end,
                            args.archive_bucket, acct, say)
        runs = runs + extra

    global ENV_PREFIXES, ENV_NAMES, ENV_PREFIX_BY_NAME
    ENV_PREFIXES = tuple(p.strip() for p in args.env_prefixes.split(",") if p.strip())
    ENV_NAMES = tuple(p.rstrip("_-.") for p in ENV_PREFIXES)
    ENV_PREFIX_BY_NAME = {p.rstrip("_-."): p for p in ENV_PREFIXES}

    data = compute(cost, jobs, runs, triggers, start, end)
    notes = build_notes(data, runs)
    rows = inventory_rows(jobs, runs, data["rate"], today)
    blob = detail_blob(jobs, runs, triggers)
    other_jobs = max(len(data["by_job"]) - len(data["top6"]), 0)
    charts = build_charts(data, notes, other_jobs)

    coverage = data["attributed"] / data["bill_qty"] * 100 if data["bill_qty"] else 0
    with_dpus = sum(1 for r in data["window"] if r.get("dpu_seconds"))
    hist = min((r["started"][:7] for r in runs), default="—")

    tokens = {
        "PERIOD": f"{month_label(start[:7])} – {month_label(sorted(data['months'])[-1])}"
                  if data["months"] else start,
        "USAGE_TYPE": data["etl"], "RATE": f"{data['rate']:.2f}",
        "BILL_USD": n(data["bill_usd"], 2), "BILL": n(data["bill_qty"]),
        "ATTR": n(data["attributed"]), "COVERAGE": f"{coverage:.1f}",
        "RESIDUAL": n(data["bill_qty"] - data["attributed"]),
        "RESID_DAY": f"{(data['bill_qty'] - data['attributed']) / max(len(data['bill_day']), 1):.2f}",
        "N_RUNS": f"{len(data['window']):,}", "N_DPUS": f"{with_dpus:,}",
        "N_EST": f"{len(data['window']) - with_dpus:,}",
        "N_JOBS_RUN": f"{len({r['job'] for r in data['window']}):,}",
        "PCT_DPUS": f"{with_dpus / max(len(data['window']), 1) * 100:.1f}",
        "PCT_EST": f"{(1 - with_dpus / max(len(data['window']), 1)) * 100:.1f}",
        "N_JOBS": f"{len(jobs):,}", "ACCOUNT": acct,
        "STALE_DAYS": STALE_DAYS, "SAMPLE_RUNS": SAMPLE_RUNS,
        "REGION": args.region, "PROFILE": args.profile,
        "SNAPSHOT": today.strftime("%Y-%m-%d"),
        "HIST_FROM": month_label(hist) if hist != "—" else "the API's retention limit",
        "FINDINGS": build_findings(data, jobs, triggers),
        "INVENTORY_TAG": f"all {len(jobs):,} · snapshot {today.strftime('%Y-%m-%d')}",
        **notes,
    }
    blobs = {
        "ENV_PREFIXES_JSON": json.dumps(list(ENV_PREFIXES)),
        "ROWS_JSON": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        "CHARTS_JSON": json.dumps(charts, ensure_ascii=False, separators=(",", ":")),
        "DETAIL_JSON": json.dumps(blob, ensure_ascii=False, separators=(",", ":")),
    }
    html = render(HTML_TEMPLATE, tokens, blobs)

    out = Path(args.out) if args.out else Path(f"glue-cost-{args.profile}.html")
    out.write_text(html, encoding="utf-8")

    say("")
    say(f"  billed      {n(data['bill_qty'])} DPU-h   {money(data['bill_usd'])}"
        f"   @ ${data['rate']:.5f}/unit  [{data['etl']}]")
    say(f"  attributed  {n(data['attributed'])} DPU-h   "
        f"{len(data['window']):,} runs across {len({r['job'] for r in data['window']})} jobs")
    say(f"  coverage    {coverage:.1f}%   residual {n(data['bill_qty'] - data['attributed'])} DPU-h")
    per_src: dict[str, float] = collections.defaultdict(float)
    for buckets in data["by_day_src"].values():
        for src, hours in buckets.items():
            per_src[src] += hours
    if len(per_src) > 1:
        label = {"api": "Glue API", "ct": "CloudTrail Event history", "s3": "CloudTrail S3 archive"}
        for src in ("api", "ct", "s3"):
            if per_src.get(src):
                say(f"    {label[src]:<26} {n(per_src[src]):>10} DPU-h")
    if coverage < 90:
        say("  ! below the 90% target — jobs were probably deleted inside the period.")
        if not (args.reconstruct or args.archive_bucket):
            say("    Re-run with --reconstruct to recover them from CloudTrail.")
        elif not args.archive_bucket:
            say("    Part of the period predates CloudTrail Event history. Find the trail bucket")
            say("    with `aws cloudtrail describe-trails` and pass --archive-bucket.")
        else:
            say("    See references/service-deep-dive.md — the remainder needs manual work.")
    if coverage > 100.5:
        say("  ! above 100% — that is a bug, not rounding. Investigate before reporting.")
    if data["crawler"] > 0.005:
        say(f"  crawlers    {money(data['crawler'])} ({n(data['crawler_h'])} DPU-h), not attributed")
    say("")
    say(f"  → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
