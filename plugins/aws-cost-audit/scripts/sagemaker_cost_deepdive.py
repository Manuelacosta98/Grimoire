#!/usr/bin/env python3
"""SageMaker cost deep dive -> self-contained HTML dashboard.

Attributes Amazon SageMaker spend down to the individual Studio space, its human
owner, the day, and the notebooks that were active in it -- then profiles what
those notebooks actually do.

Cost Explorer stops at SERVICE/USAGE_TYPE. This reconstructs the layer below it
from CloudTrail app lifecycle events (exact, but only as far back as CloudTrail
Event history reaches) and CloudWatch Logs Insights occupancy (the whole history,
because Studio's log group typically has no retention limit).

Dollars are always *shares of the billed amount*, never hours x rate, so every
total reconciles to the bill by construction. Hours coverage is reported
separately as the attribution-quality metric -- see --help and the skill's
references/attribution-method.md.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required:  pip install boto3")

from sagemaker_cost_html import render


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Every IAM action this script calls against the *workload* account. CI diffs this
# list against iam/cost-audit-policy.json and iam/cost-audit-role.yaml. All reads.
REQUIRED_ACTIONS = [
    "ce:GetCostAndUsage",              # billed hours and dollars per usage type
    "cloudtrail:DescribeTrails",       # is there a trail reaching far enough back
    "cloudtrail:ListEventDataStores",
    "cloudtrail:LookupEvents",         # app lifecycle: the exact occupancy spans
    "datazone:ListDomains",            # Studio user profiles are DataZone GUIDs
    "datazone:ListProjects",
    "datazone:SearchUserProfiles",     # the GUID -> human name mapping
    "logs:DescribeLogGroups",
    "logs:GetQueryResults",
    "logs:StartQuery",                 # Logs Insights occupancy, beyond CloudTrail reach
    "s3:GetObject",                    # reading notebooks to profile what they run
    "s3:ListAllMyBuckets",             # locating the Studio mirror bucket
    "s3:ListBucket",
    "sagemaker:ListDomains",
    "sagemaker:ListSpaces",
    "sagemaker:ListUserProfiles",
    "sts:GetCallerIdentity",
]

# Payer-account only, used through --payer-profile and never through the audit role:
#   ce:ListCostAllocationTags, ce:GetCostAndUsageWithResources,
#   cur:DescribeReportDefinitions, bcm-data-exports:ListExports
# A member account cannot read these at any permission level, so their absence is
# an answer, not a permissions bug. They are written without quotes on purpose --
# they are documentation, not a claim on the least-privilege role.

SERVICE = "Amazon SageMaker"
STUDIO_LOG_GROUP = "/aws/sagemaker/studio"

# CloudTrail Event history retains 90 days. Leave a day of slack.
CLOUDTRAIL_LOOKBACK_DAYS = 89

# Usage-type families. `attribution` names the reconstruction strategy;
# `validated` records whether that strategy has been reconciled against a real
# bill. Never let the report imply coverage for an unvalidated family --
# meta.unvalidated_kinds surfaces them in the dashboard banner.
RESOURCE_MAP = [
    {"kind": "JupyterLab", "attribution": "studio_app", "validated": True,
     "note": "Studio / Unified Studio JupyterLab space. CreateApp/DeleteApp + Studio log occupancy."},
    {"kind": "CodeEditor", "attribution": "studio_app", "validated": False,
     "note": "Same app lifecycle as JupyterLab; not yet reconciled against a bill."},
    {"kind": "Notebook", "attribution": "studio_app", "validated": False,
     "note": "Unified Studio Notebook. Log streams are Notebook/<id>, not domain/space."},
    {"kind": "KernelGateway", "attribution": "studio_app", "validated": False,
     "note": "Studio Classic kernel app."},
    {"kind": "Notebk", "attribution": "notebook_instance", "validated": False,
     "note": "Classic notebook instance. Needs Create/Start/StopNotebookInstance events."},
    {"kind": "ML-Instance", "attribution": "job", "validated": False,
     "note": "Training/processing job. Use per-job BillableTimeInSeconds instead of spans."},
    {"kind": "Host", "attribution": "endpoint", "validated": False,
     "note": "Real-time endpoint. Needs a DescribeEndpointConfig timeline."},
]
ATTRIB_BY_KIND = {r["kind"]: r for r in RESOURCE_MAP}

# Kinds whose billed unit is time and which this tool attributes to app spans.
STUDIO_APP_KINDS = {k for k, r in ATTRIB_BY_KIND.items() if r["attribution"] == "studio_app"}

# Cost-allocation tags SageMaker Unified Studio already stamps on every app.
# Activating these in the payer account makes per-project/per-user cost exact
# for *future* periods -- tag activation is never retroactive.
DATAZONE_TAG_KEYS = ["AmazonDataZoneProject", "AmazonDataZoneUser", "AmazonDataZoneDomain"]

# Notebook code profiling
CODE_LIB_HINTS = [
    "pandas", "numpy", "polars", "pyspark", "dask", "sklearn", "scikit-learn",
    "xgboost", "lightgbm", "catboost", "statsmodels", "optuna", "hyperopt",
    "torch", "tensorflow", "keras", "prophet", "shap", "matplotlib", "seaborn",
    "plotly", "boto3", "awswrangler", "psycopg2", "sqlalchemy", "redshift_connector",
    "snowflake", "duckdb", "geopandas", "networkx", "transformers",
]
CODE_HEAVY_HINTS = [
    "read_parquet", "read_sql", "read_csv", "to_parquet", "GridSearchCV",
    "RandomizedSearchCV", "cross_val_score", "create_study", "optimize(",
    "n_jobs=-1", "fit(", "predict(", "SparkSession", "LocalCluster",
    "joblib", "multiprocessing", "Tweedie", "GLM", "groupby", "merge(",
]
# Notebooks in the corpus this was validated against run to 87 MB, almost all of
# it saved cell output.
# Above FULL_PARSE_BYTES a json.loads costs ~1 GB of RAM for data we discard,
# so those get a regex-only pass for the execution timings -- the one field the
# cost allocation actually depends on.
NB_MAX_BYTES = 400 * 1024 * 1024
FULL_PARSE_BYTES = 25 * 1024 * 1024
CODE_FILE_SUFFIXES = (".ipynb", ".py", ".sql", ".r", ".scala")

# Allocation weight tiers, best first. Every allocated row records which one
# produced it, so a weak split is never presented as a strong one.
TIER_EXEC_SECONDS = "exec-seconds"   # real executions x the notebook's own sec/cell
TIER_EXEC_COUNT = "exec-count"       # real executions x the corpus median sec/cell
TIER_EDITOR_SYNC = "editor-sync"     # editor open/sync events -- weakest, last resort
TIER_RANK = {TIER_EXEC_SECONDS: 0, TIER_EXEC_COUNT: 1, TIER_EDITOR_SYNC: 2}

# Fallback when a notebook has executions but no usable timing profile.
DEFAULT_SEC_PER_CELL = 3.0

# A timing profile has to be worth trusting before it outranks the corpus
# median. Both gates below were set from the measured distribution, not tuned:
# of 100 profiled notebooks in that corpus exactly one failed them -- a 9 KB notebook whose
# three timed cells averaged 0.0001 s, which would have priced its 13 real cell
# runs at $0.00. Too thin a sample and too coarse a resolution are both reasons
# to fall back, not reasons to trust a zero.
MIN_TIMED_CELLS = 5
MIN_SEC_PER_CELL = 0.01

# The notebook parse cache is keyed by S3 ETag, which tracks the *file* but not
# the *shape of what we derive from it*. Bump this whenever a profile field is
# added, or a stale cache will silently serve profiles missing the new field --
# which is exactly how the first kernel-second run came out 87% exec-count.
PROFILE_SCHEMA = 2


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Time helpers -- every timestamp is normalised to UTC before any comparison.
# CloudTrail Event history returns tz-aware local offsets while Insights bin()
# returns naive UTC; comparing them raw silently mis-dates everything.
# --------------------------------------------------------------------------- #

def to_utc(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        d = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            d = dt.datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S")
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def day_str(d: dt.datetime) -> str:
    return d.date().isoformat()


def split_by_utc_day(start: dt.datetime, end: dt.datetime):
    """Yield (day_string, hours) splitting a span at UTC midnight."""
    cur = start
    while cur < end:
        nxt_midnight = dt.datetime.combine(
            cur.date() + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
        )
        seg_end = min(end, nxt_midnight)
        yield day_str(cur), (seg_end - cur).total_seconds() / 3600.0
        cur = seg_end


def month_chunks(start: dt.datetime, end: dt.datetime):
    cur = start
    while cur < end:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1)
        nxt = nxt.replace(hour=0, minute=0, second=0, microsecond=0)
        yield cur, min(nxt, end)
        cur = nxt


# --------------------------------------------------------------------------- #
# Usage-type parsing
# --------------------------------------------------------------------------- #

USAGE_RE = re.compile(
    r"^(?:(?P<region>[A-Z][A-Z0-9]{2,5})-)?(?P<component>[A-Za-z]+)"
    r"(?::(?P<kind>[A-Za-z\-]+?)(?:-(?P<instance>(?:ml|sc|db)\.[A-Za-z0-9\.]+))?)?$"
)


def parse_usage_type(usage_type: str) -> dict:
    """`USE1-Studio:JupyterLab-ml.m7i.48xlarge` -> its parts.

    Falls back to kind=<raw> rather than guessing, so an unrecognised usage type
    shows up in the report as unattributed instead of silently vanishing.
    """
    out = {"usage_type": usage_type, "region": None, "component": None,
           "kind": None, "instance": None}
    m = USAGE_RE.match(usage_type)
    if m:
        out.update({k: m.group(k) for k in ("region", "component", "kind", "instance")})
    if out["kind"] is None and ":" in usage_type:
        left, right = usage_type.split(":", 1)
        out["component"] = left.split("-")[-1]
        if "-" in right:
            kind, rest = right.split("-", 1)
            if re.match(r"^(ml|sc|db)\.", rest):
                out["kind"], out["instance"] = kind, rest
            else:
                out["kind"] = right
        else:
            out["kind"] = right
    if out["kind"] is None:
        out["kind"] = usage_type
    return out


def is_storage_kind(kind: str) -> bool:
    return kind.startswith("VolumeUsage") or "Storage" in kind


def is_app_kind(kind: str) -> bool:
    return kind in STUDIO_APP_KINDS


# --------------------------------------------------------------------------- #
# AWS plumbing
# --------------------------------------------------------------------------- #

class Aws:
    """Workload-account clients, plus optional payer clients that degrade
    to None rather than aborting the run."""

    def __init__(self, profile: str, region: str, payer_profile: str | None = None):
        self.region = region
        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.account = None
        self.warnings: list[str] = []
        try:
            self.account = self.session.client("sts").get_caller_identity()["Account"]
        except Exception as exc:
            sys.exit(f"cannot authenticate profile {profile!r}: {exc}")

        self.payer_session = None
        self.payer_account = None
        self.payer_profile = payer_profile
        self.payer_mode = None
        if payer_profile:
            try:
                ps = boto3.Session(profile_name=payer_profile, region_name=region)
                self.payer_account = ps.client("sts").get_caller_identity()["Account"]
                self.payer_session, self.payer_mode = ps, "boto3"
            except Exception as exc:
                # Some profile types (notably AWS CLI `login_session`) work in the
                # CLI but not in boto3 without extra dependencies. The CLI carries
                # its own, so shell out rather than lose the payer view.
                ident = cli_json(payer_profile, ["sts", "get-caller-identity"], region)
                if ident and ident.get("Account"):
                    self.payer_account, self.payer_mode = ident["Account"], "cli"
                    log(f"  payer profile {payer_profile!r} not usable via boto3 "
                        f"({_short(exc)}); using the aws CLI instead")
                else:
                    self.warn(f"payer profile {payer_profile!r} unusable ({_short(exc)}); "
                              "continuing in workload-only mode")

    def warn(self, msg: str) -> None:
        log(f"  ! {msg}")
        self.warnings.append(msg)

    def c(self, name: str, **kw):
        return self.session.client(name, **kw)


def cli_json(profile: str, args: list[str], region: str | None = None,
             extra: list[str] | None = None) -> dict | None:
    """Run an aws CLI call and parse its JSON. Used only as a fallback for
    profile types boto3 cannot load in this interpreter."""
    cmd = ["aws"] + args + ["--profile", profile, "--output", "json"]
    if region:
        cmd += ["--region", region]
    if extra:
        cmd += extra
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return {"__error__": (res.stderr or res.stdout).strip().split("\n")[-1][:200]}
    try:
        return json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return None


def _short(exc: Exception) -> str:
    s = str(exc)
    if isinstance(exc, ClientError):
        s = exc.response.get("Error", {}).get("Code", "") + ": " + \
            exc.response.get("Error", {}).get("Message", s)
    return s.split("\n")[0][:200]


# --------------------------------------------------------------------------- #
# Stage 1 -- billed truth from Cost Explorer
# --------------------------------------------------------------------------- #

def fetch_billed(aws: Aws, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Daily cost + usage quantity per usage type. This is the only source of
    dollars in the whole report."""
    ce = aws.c("ce", region_name="us-east-1")
    flt = {"And": [
        {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}},
        {"Dimensions": {"Key": "SERVICE", "Values": [SERVICE]}},
    ]}
    rows, token = [], None
    while True:
        kw = dict(
            TimePeriod={"Start": day_str(start), "End": day_str(end)},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter=flt,
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        if token:
            kw["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kw)
        for period in resp["ResultsByTime"]:
            day = period["TimePeriod"]["Start"]
            for grp in period.get("Groups", []):
                cost = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                qty = float(grp["Metrics"]["UsageQuantity"]["Amount"])
                if cost == 0 and qty == 0:
                    continue
                rec = parse_usage_type(grp["Keys"][0])
                rec.update({"day": day, "cost": cost, "qty": qty,
                            "rate": (cost / qty) if qty else 0.0})
                rows.append(rec)
        token = resp.get("NextPageToken")
        if not token:
            break
    log(f"  billed rows: {len(rows)}  "
        f"(${sum(r['cost'] for r in rows):,.2f} over {day_str(start)}..{day_str(end)})")
    return rows


# --------------------------------------------------------------------------- #
# Stage 2 -- identity: space -> owner -> project, in human names
# --------------------------------------------------------------------------- #

def fetch_identity(aws: Aws) -> dict:
    sm = aws.c("sagemaker")
    domains, spaces, profiles = {}, {}, {}

    try:
        for page in sm.get_paginator("list_domains").paginate():
            for d in page["Domains"]:
                domains[d["DomainId"]] = d.get("DomainName") or d["DomainId"]
    except Exception as exc:
        aws.warn(f"list_domains failed ({_short(exc)})")

    try:
        for page in sm.get_paginator("list_spaces").paginate():
            for s in page["Spaces"]:
                own = (s.get("OwnershipSettingsSummary") or {}).get("OwnerUserProfileName")
                settings = s.get("SpaceSettingsSummary") or {}
                key = space_key(s["DomainId"], s["SpaceName"])
                spaces[key] = {
                    "domain_id": s["DomainId"],
                    "space": s["SpaceName"],
                    "owner_uuid": own,
                    "app_type": settings.get("AppType"),
                    "ebs_gb": ((settings.get("SpaceStorageSettings") or {})
                               .get("EbsStorageSettings") or {}).get("EbsVolumeSizeInGb"),
                    "created": str(s.get("CreationTime") or ""),
                    "exists": True,
                }
    except Exception as exc:
        aws.warn(f"list_spaces failed ({_short(exc)})")

    try:
        for page in sm.get_paginator("list_user_profiles").paginate():
            for p in page["UserProfiles"]:
                profiles[p["UserProfileName"]] = p["DomainId"]
    except Exception as exc:
        aws.warn(f"list_user_profiles failed ({_short(exc)})")

    # DataZone turns the UUIDs into people. Without it every name is a GUID.
    users, projects, dz_domains = {}, {}, {}
    try:
        dz = aws.c("datazone")
        for page in dz.get_paginator("list_domains").paginate():
            for d in page["items"]:
                dz_domains[d["id"]] = d.get("name") or d["id"]
        for dz_id in dz_domains:
            for utype in ("DATAZONE_USER", "DATAZONE_SSO_USER", "DATAZONE_IAM_USER"):
                try:
                    tok = None
                    while True:
                        kw = dict(domainIdentifier=dz_id, userType=utype, maxResults=50)
                        if tok:
                            kw["nextToken"] = tok
                        r = dz.search_user_profiles(**kw)
                        for u in r.get("items", []):
                            det = u.get("details") or {}
                            sso, iam = det.get("sso") or {}, det.get("iam") or {}
                            # IAM ARNs end in /<user> for users but :root for
                            # the account root, so split on both separators.
                            arn_tail = (iam.get("arn") or "").rsplit("/", 1)[-1]
                            arn_tail = arn_tail.rsplit(":", 1)[-1]
                            name = (sso.get("username")
                                    or " ".join(x for x in (sso.get("firstName"),
                                                            sso.get("lastName")) if x).strip()
                                    or arn_tail)
                            if u.get("id") and name:
                                users.setdefault(u["id"], name)
                        tok = r.get("nextToken")
                        if not tok:
                            break
                except Exception:
                    continue
            try:
                for page in dz.get_paginator("list_projects").paginate(domainIdentifier=dz_id):
                    for p in page.get("items", []):
                        projects[p["id"]] = p.get("name") or p["id"]
            except Exception:
                pass
    except Exception as exc:
        aws.warn(f"DataZone lookup failed ({_short(exc)}); owners stay as UUIDs")

    log(f"  identity: {len(domains)} domains, {len(spaces)} spaces, "
        f"{len(users)} users, {len(projects)} projects")
    return {"domains": domains, "spaces": spaces, "profiles": profiles,
            "users": users, "projects": projects, "dz_domains": dz_domains}


def space_key(domain_id: str, space_name: str) -> str:
    """Case-normalised. `d-x/Space1` and `d-x/space1` are the same space but
    appear as two distinct CloudWatch log streams; grouping on the raw name
    double-counts it."""
    return f"{domain_id}/{(space_name or '').lower()}"


# --------------------------------------------------------------------------- #
# Stage 3a -- app spans from CloudTrail (exact instance type, ~90 days)
# --------------------------------------------------------------------------- #

def fetch_cloudtrail_spans(aws: Aws, start: dt.datetime, end: dt.datetime,
                           workers: int) -> tuple[list[dict], dict]:
    ct_floor = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CLOUDTRAIL_LOOKBACK_DAYS)
    lookup_start = max(start, ct_floor)
    info = {"available": False, "from": day_str(lookup_start), "to": day_str(end),
            "clamped": lookup_start > start, "trail_archive": False, "events": 0}

    ctl = aws.c("cloudtrail")
    try:
        trails = ctl.describe_trails(includeShadowTrails=True).get("trailList", [])
        eds = ctl.list_event_data_stores().get("EventDataStores", [])
        info["trail_archive"] = bool(trails)
        info["trails"] = [t.get("Name") for t in trails]
        info["event_data_stores"] = [e.get("Name") for e in eds]
    except Exception as exc:
        aws.warn(f"describe_trails failed ({_short(exc)})")

    if lookup_start >= end:
        return [], info

    # Sweep one UTC day per worker. A single wide lookup_events call hits
    # pagination caps and truncates newest-first without any error.
    days = []
    cur = dt.datetime.combine(lookup_start.date(), dt.time.min, tzinfo=dt.timezone.utc)
    while cur < end:
        days.append(cur)
        cur += dt.timedelta(days=1)

    # lookup_events is rate-limited to roughly 2 requests/second per account.
    # A swallowed ThrottlingException silently truncates a day, which shows up
    # later as an app with no DeleteApp -- an open span that invents hours. So
    # retry with backoff, and if a sweep still fails, say so and stop treating
    # CloudTrail as complete.
    shared = aws.c("cloudtrail")
    failures: list[str] = []

    def sweep(day: dt.datetime, event_name: str) -> list[dict]:
        out, tok = [], None
        while True:
            kw = dict(
                LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": event_name}],
                StartTime=day, EndTime=day + dt.timedelta(days=1), MaxResults=50)
            if tok:
                kw["NextToken"] = tok
            r = None
            for attempt in range(7):
                try:
                    r = shared.lookup_events(**kw)
                    break
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code in ("ThrottlingException", "RequestLimitExceeded",
                                "TooManyRequestsException", "SlowDown"):
                        time.sleep(min(0.6 * (2 ** attempt), 20) * (0.6 + 0.8 * (attempt % 2)))
                        continue
                    failures.append(f"{day_str(day)}/{event_name}: {code}")
                    return out
                except (BotoCoreError, OSError) as exc:
                    time.sleep(min(0.6 * (2 ** attempt), 20))
                    continue
            if r is None:
                failures.append(f"{day_str(day)}/{event_name}: throttled out")
                return out
            for e in r["Events"]:
                try:
                    out.append(json.loads(e["CloudTrailEvent"]))
                except Exception:
                    continue
            tok = r.get("NextToken")
            if not tok:
                return out

    creates, deletes = [], []
    ct_workers = max(1, min(workers, 4))  # low, deliberately: see above
    with ThreadPoolExecutor(max_workers=ct_workers) as pool:
        futs = {}
        for day in days:
            futs[pool.submit(sweep, day, "CreateApp")] = "c"
            futs[pool.submit(sweep, day, "DeleteApp")] = "d"
        for fut in as_completed(futs):
            (creates if futs[fut] == "c" else deletes).extend(fut.result())

    # Events can be returned more than once across paginated sweeps.
    creates = _dedupe_events(creates)
    deletes = _dedupe_events(deletes)

    info["available"] = bool(creates or deletes)
    info["events"] = len(creates) + len(deletes)
    info["complete"] = not failures
    info["failed_sweeps"] = failures[:20]
    if failures:
        aws.warn(f"{len(failures)} CloudTrail day-sweep(s) failed; span data is "
                 f"incomplete, so log occupancy is used for hours instead")

    def app_id(ev) -> tuple | None:
        rp = ev.get("requestParameters") or {}
        if not rp.get("spaceName") or not rp.get("domainId"):
            return None
        return (rp["domainId"], (rp["spaceName"] or "").lower(),
                rp.get("appType"), rp.get("appName"))

    starts = collections.defaultdict(list)
    for ev in creates:
        if ev.get("errorCode"):
            continue  # a rejected call never ran
        key = app_id(ev)
        if not key:
            continue
        rp = ev["requestParameters"]
        tags = {t["key"]: t["value"] for t in (rp.get("tags") or []) if "key" in t}
        starts[key].append({
            "at": to_utc(ev["eventTime"]),
            "instance": (rp.get("resourceSpec") or {}).get("instanceType"),
            "image": (rp.get("resourceSpec") or {}).get("sageMakerImageArn"),
            "tags": tags,
            "actor": ((ev.get("userIdentity") or {}).get("sessionContext") or {})
                     .get("sourceIdentity") or (ev.get("userIdentity") or {}).get("userName"),
        })

    ends = collections.defaultdict(list)
    for ev in deletes:
        if ev.get("errorCode"):
            continue
        key = app_id(ev)
        if key:
            ends[key].append(to_utc(ev["eventTime"]))

    spans = []
    for key, opens in starts.items():
        closes = sorted(ends.get(key, []))
        for op in sorted(opens, key=lambda x: x["at"]):
            later = [c for c in closes if c > op["at"]]
            close = later[0] if later else end
            spans.append({
                "space_key": f"{key[0]}/{key[1]}",
                "domain_id": key[0], "space": key[1],
                "app_type": key[2], "app_name": key[3],
                "start": op["at"], "end": min(close, end),
                "instance": op["instance"], "image": op["image"],
                "tags": op["tags"], "actor": op["actor"],
                "closed": bool(later),
            })

    log(f"  cloudtrail: {len(creates)} CreateApp / {len(deletes)} DeleteApp "
        f"-> {len(spans)} spans from {info['from']}"
        + ("  [window clamped to Event history]" if info["clamped"] else ""))
    return spans, info


# --------------------------------------------------------------------------- #
# Stage 3b -- occupancy from CloudWatch Logs Insights (the whole history)
# --------------------------------------------------------------------------- #

def run_insights(aws: Aws, group: str, query: str, start: dt.datetime,
                 end: dt.datetime, limit: int = 10000,
                 poll: float = 1.5, timeout: float = 900) -> list[dict]:
    """One Insights query. Never paginate filter_log_events over a period this
    wide -- a single stream-month is ~125 pages and minutes of wall clock,
    where Insights aggregates the whole log group server-side in seconds."""
    logs = aws.c("logs")
    try:
        qid = logs.start_query(logGroupName=group, startTime=int(start.timestamp()),
                               endTime=int(end.timestamp()), queryString=query,
                               limit=limit)["queryId"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            aws.warn(f"log group {group} not found")
            return []
        if code == "MalformedQueryException":
            # The window falls outside the group's lifetime or retention. That
            # is information, not a failure: there are no logs to find there.
            log(f"    {day_str(start)}..{day_str(end)}: outside the log group's "
                f"retained range, skipped")
            return []
        raise
    deadline = time.time() + timeout
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        if time.time() > deadline:
            try:
                logs.stop_query(queryId=qid)
            except Exception:
                pass
            aws.warn(f"Insights query timed out after {timeout:.0f}s")
            return []
        time.sleep(poll)
    if r["status"] != "Complete":
        aws.warn(f"Insights query ended {r['status']}")
    return [{c["field"]: c["value"] for c in row} for row in r["results"]]


def insights_adaptive(aws: Aws, group: str, query: str, start: dt.datetime,
                      end: dt.datetime, limit: int = 10000) -> list[dict]:
    """Run a stats query over [start,end), halving the window whenever a chunk
    saturates the row limit. Truncation in Insights is silent, so treat a
    near-full result as untrustworthy rather than complete."""
    rows = run_insights(aws, group, query, start, end, limit=limit)
    if len(rows) < limit * 0.9 or (end - start) <= dt.timedelta(days=1):
        if len(rows) >= limit * 0.9:
            aws.warn(f"Insights hit the row cap on a single day "
                     f"({day_str(start)}); results may be truncated")
        return rows
    mid = start + (end - start) / 2
    mid = dt.datetime.combine(mid.date(), dt.time.min, tzinfo=dt.timezone.utc)
    if mid <= start or mid >= end:
        return rows
    log(f"    row cap hit for {day_str(start)}..{day_str(end)}; splitting")
    return (insights_adaptive(aws, group, query, start, mid, limit)
            + insights_adaptive(aws, group, query, mid, end, limit))


APP_STREAM_FILTER = (
    r"@logStream like /\/(JupyterLab|CodeEditor|KernelGateway|TensorBoard|RSession)\/[^\/]+$/"
    r" or @logStream like /^Notebook\/[^\/]+$/"
)


def fetch_log_occupancy(aws: Aws, start: dt.datetime, end: dt.datetime,
                        bin_minutes: int, gap_bins: int) -> tuple[dict, dict]:
    """Per-space active hours per day, from binned log occupancy.

    A Studio app writes to its log stream continuously while it runs, so
    contiguous non-empty bins are a running app. The trailing bin is counted
    whole, which is part of why coverage lands slightly above 100%.
    """
    query = f"""
fields @timestamp, @logStream
| filter {APP_STREAM_FILTER}
| stats count(*) as n by @logStream, bin({bin_minutes}m) as b
| sort b asc
| limit 10000
"""
    # Querying before the group existed is a hard error, not an empty result,
    # so clamp to its creation time and to its retention window.
    floor = start
    retention_days = None
    try:
        for g in aws.c("logs").describe_log_groups(
                logGroupNamePrefix=STUDIO_LOG_GROUP)["logGroups"]:
            if g["logGroupName"] != STUDIO_LOG_GROUP:
                continue
            created = to_utc(dt.datetime.fromtimestamp(
                g["creationTime"] / 1000, tz=dt.timezone.utc))
            floor = max(floor, created)
            retention_days = g.get("retentionInDays")
            if retention_days:
                floor = max(floor, dt.datetime.now(dt.timezone.utc)
                            - dt.timedelta(days=retention_days))
    except Exception as exc:
        aws.warn(f"could not read {STUDIO_LOG_GROUP} metadata ({_short(exc)})")
    if floor > start:
        why = (f"retention is {retention_days} days" if retention_days
               else "the log group did not exist before then")
        log(f"  occupancy window starts {day_str(floor)} — {why}")
    if floor >= end:
        aws.warn(f"the whole period predates {STUDIO_LOG_GROUP}; no occupancy available")
        return {}, {"streams": [], "sessions": [], "bin_minutes": bin_minutes,
                    "gap_bins": gap_bins, "window_start": day_str(floor),
                    "retention_days": retention_days}

    rows: list[dict] = []
    for chunk_start, chunk_end in month_chunks(floor, end):
        got = insights_adaptive(aws, STUDIO_LOG_GROUP, query, chunk_start, chunk_end)
        log(f"    {day_str(chunk_start)}: {len(got)} occupancy bins")
        rows.extend(got)

    bins_by_stream: dict[str, list[dt.datetime]] = collections.defaultdict(list)
    for r in rows:
        stream, b = r.get("@logStream"), r.get("b")
        if not stream or not b:
            continue
        bins_by_stream[stream.lower()].append(to_utc(b))

    bin_delta = dt.timedelta(minutes=bin_minutes)
    gap_tol = bin_delta * max(gap_bins, 1)
    per_day: dict[tuple[str, str], float] = collections.Counter()
    sessions: list[dict] = []

    for stream, bins in bins_by_stream.items():
        bins.sort()
        clusters = [[bins[0], bins[0]]]
        for t in bins[1:]:
            if t - clusters[-1][1] > gap_tol:
                clusters.append([t, t])
            else:
                clusters[-1][1] = t
        sk = stream_to_space_key(stream)
        for lo, hi in clusters:
            hi_end = hi + bin_delta
            sessions.append({"space_key": sk, "stream": stream,
                             "start": lo, "end": hi_end,
                             "hours": (hi_end - lo).total_seconds() / 3600.0})
            for day, hours in split_by_utc_day(lo, hi_end):
                per_day[(sk, day)] += hours

    log(f"  log occupancy: {len(bins_by_stream)} streams, {len(sessions)} sessions, "
        f"{sum(per_day.values()):.1f} active hours")
    return per_day, {"streams": sorted(bins_by_stream), "sessions": sessions,
                     "bin_minutes": bin_minutes, "gap_bins": gap_bins,
                     "window_start": day_str(floor), "retention_days": retention_days}


def stream_to_space_key(stream: str) -> str:
    """`d-xxxx/space-name/JupyterLab/default` -> `d-xxxx/space-name`.
    `Notebook/<id>` has no domain, so it becomes its own pseudo-space."""
    parts = stream.split("/")
    if len(parts) >= 3 and parts[0].startswith("d-"):
        return f"{parts[0]}/{parts[1]}"
    return stream if len(parts) < 2 else "/".join(parts[:2])


def _dedupe_events(events: list[dict]) -> list[dict]:
    seen, out = set(), []
    for ev in events:
        eid = ev.get("eventID") or (ev.get("eventTime"), ev.get("requestID"))
        if eid in seen:
            continue
        seen.add(eid)
        out.append(ev)
    return out


def clip_open_spans(ct_spans: list[dict], sessions: list[dict],
                    period_end: dt.datetime) -> int:
    """An app whose DeleteApp is missing (still running, or stopped outside the
    lookup window) has no end. Left open it runs to the end of the period and
    invents days of runtime -- the single largest source of over-attribution.

    The end comes from the space's own log activity: the last occupancy cluster
    that began during this span. Absent logs, fall back to the median closed
    span and mark it estimated.
    """
    now = dt.datetime.now(dt.timezone.utc)
    ceiling = min(period_end, now)

    clusters: dict[str, list[tuple[dt.datetime, dt.datetime]]] = collections.defaultdict(list)
    for sess in sessions:
        clusters[sess["space_key"]].append((sess["start"], sess["end"]))
    for lst in clusters.values():
        lst.sort()

    starts_by_space: dict[str, list[dt.datetime]] = collections.defaultdict(list)
    for sp in ct_spans:
        starts_by_space[sp["space_key"]].append(sp["start"])
    for lst in starts_by_space.values():
        lst.sort()

    closed = [(sp["end"] - sp["start"]).total_seconds()
              for sp in ct_spans if sp.get("closed")]
    fallback = statistics.median(closed) if closed else 3600.0

    SLACK = dt.timedelta(minutes=10)
    fixed = 0
    for sp in ct_spans:
        if sp.get("closed"):
            sp["end_source"] = "cloudtrail"
            sp["end"] = min(sp["end"], ceiling)
            continue
        nxt = [t for t in starts_by_space[sp["space_key"]] if t > sp["start"]]
        window_end = min(nxt[0], ceiling) if nxt else ceiling
        overlap = [c_end for c_start, c_end in clusters.get(sp["space_key"], [])
                   if c_start >= sp["start"] - SLACK and c_start < window_end]
        if overlap:
            sp["end"] = min(max(overlap), window_end)
            sp["end_source"] = "logs"
        else:
            sp["end"] = min(sp["start"] + dt.timedelta(seconds=fallback), window_end)
            sp["end_source"] = "median-duration"
        if sp["end"] < sp["start"]:
            sp["end"] = sp["start"]
        fixed += 1
    if fixed:
        log(f"  clipped {fixed} open span(s) to their last observed log activity")
    return fixed


# --------------------------------------------------------------------------- #
# Stage 4/5 -- attribute each day's billed hours to spaces, then price them
# --------------------------------------------------------------------------- #

def attribute_days(billed: list[dict], ct_spans: list[dict], occupancy: dict,
                   ct_complete: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
    """Partition each day's billed instance-hours among the spaces active that day.

    A space-day is NOT one instance type. An app gets deleted and recreated on a
    bigger instance mid-afternoon, so one space can consume several types in a
    day -- and pinning one type per space-day leaves whole types with nobody on
    them (real spend, reported as unattributed) while another type shows 280%
    coverage. Both were happening before this was modelled as a partition.

    Procedure, per UTC day:
      1. Build the billed pool {instance_type: hours} and the observed hours per
         space (CloudTrail spans where trustworthy, else log occupancy).
      2. Consume the pool with CloudTrail's exact (space, type, hours) -- the
         only place the type is directly observed.
      3. Rank-match the remainder: the space with the most remaining hours draws
         from the type with the most remaining pool hours, taking the smaller of
         the two, until one side is exhausted. Hours are the only signal left, so
         pairing by rank in hours is the most that signal supports; it is
         recorded as `rank-match` on every row it produces.

    Dollars follow the partition, so a day's allocated cost equals its billed
    cost exactly. The honest error bar is `observed_coverage` -- observed hours
    against billed hours -- which is reported per day and never tuned.
    """
    # --- observed hours per (space, day)
    ct_hours: dict[tuple[str, str], float] = collections.Counter()
    ct_typed: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for sp in ct_spans:
        for day, hours in split_by_utc_day(sp["start"], sp["end"]):
            ct_hours[(sp["space_key"], day)] += hours
            if sp.get("instance"):
                ct_typed[(sp["space_key"], day)][sp["instance"]] += hours

    observed: dict[tuple[str, str], tuple[float, str]] = {}
    if ct_complete:
        for key, h in ct_hours.items():
            observed[key] = (h, "cloudtrail")
        for key, h in occupancy.items():
            observed.setdefault(key, (h, "logs"))
    else:
        # A failed sweep can drop a DeleteApp, and an open span over-attributes
        # far worse than a quiet log gap under-attributes.
        for key, h in occupancy.items():
            observed[key] = (h, "logs")
        for key, h in ct_hours.items():
            observed.setdefault(key, (h, "cloudtrail-partial"))

    # --- billed pool per (day, instance type)
    pool: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for row in billed:
        if not (is_app_kind(row["kind"]) and row["instance"]):
            continue
        cell = pool[row["day"]].setdefault(row["instance"],
                                           {"hours": 0.0, "cost": 0.0,
                                            "usage_type": row["usage_type"]})
        cell["hours"] += row["qty"]
        cell["cost"] += row["cost"]

    obs_by_day: dict[str, dict[str, tuple[float, str]]] = collections.defaultdict(dict)
    for (sk, day), val in observed.items():
        if val[0] > 0:
            obs_by_day[day][sk] = val

    space_days: list[dict] = []
    recon: list[dict] = []
    notes: list[dict] = []

    for day in sorted(set(pool) | set(obs_by_day)):
        types = {t: dict(v) for t, v in pool.get(day, {}).items()}
        spaces = dict(obs_by_day.get(day, {}))
        rate = {t: (v["cost"] / v["hours"] if v["hours"] else 0.0) for t, v in types.items()}
        remaining_type = {t: v["hours"] for t, v in types.items()}
        remaining_space = {sk: v[0] for sk, v in spaces.items()}
        alloc: dict[tuple[str, str], list] = {}  # (space, type) -> [hours, source]

        def give(sk, t, hours, src):
            if hours <= 1e-9:
                return
            cell = alloc.setdefault((sk, t), [0.0, src])
            cell[0] += hours
            if cell[1] != src:
                cell[1] = "mixed"
            remaining_type[t] -= hours
            remaining_space[sk] -= hours

        # 1. CloudTrail's directly observed types
        for sk in list(spaces):
            for t, h in ct_typed.get((sk, day), {}).items():
                if t not in remaining_type:
                    continue
                give(sk, t, min(h, remaining_type[t], remaining_space[sk]), "cloudtrail")

        # 2. rank-match whatever is left
        guard = 0
        while True:
            guard += 1
            if guard > 500:
                notes.append({"day": day, "issue": "rank-match did not converge"})
                break
            sk_pool = [(h, sk) for sk, h in remaining_space.items() if h > 1e-6]
            t_pool = [(h, t) for t, h in remaining_type.items() if h > 1e-6]
            if not sk_pool or not t_pool:
                break
            sk_pool.sort(reverse=True)
            t_pool.sort(reverse=True)
            (sh, sk), (th, t) = sk_pool[0], t_pool[0]
            give(sk, t, min(sh, th), "rank-match")

        single_type = len(types) == 1
        for (sk, t), (hours, src) in sorted(alloc.items()):
            if src == "rank-match" and single_type:
                src = "single-type-day"
            space_days.append({
                "day": day, "space_key": sk, "instance": t,
                "hours": round(hours, 4),
                "observed_hours": round(spaces[sk][0], 4),
                "cost": round(hours * rate[t], 6),
                "hours_source": spaces[sk][1],
                "type_source": src,
            })

        observed_total = sum(v[0] for v in spaces.values())
        for t, v in sorted(types.items()):
            allocated = sum(h for (sk, tt), (h, _) in alloc.items() if tt == t)
            unpriced = max(0.0, v["hours"] - allocated)
            recon.append({
                "day": day, "instance": t,
                "billed_hours": round(v["hours"], 4),
                "billed_cost": round(v["cost"], 6),
                "allocated_hours": round(allocated, 4),
                "observed_hours": round(observed_total, 4),
                "rate": round(rate[t], 6),
                "spaces": len({sk for (sk, tt) in alloc if tt == t}),
                "coverage": round(allocated / v["hours"], 4) if v["hours"] else None,
                "unattributed_cost": round(unpriced * rate[t], 6),
            })
        leftover_space = sum(h for h in remaining_space.values() if h > 1e-6)
        if leftover_space > 0.05:
            notes.append({"day": day, "issue": "observed hours exceed billed hours",
                          "excess_hours": round(leftover_space, 3)})

    tot_billed = sum(r["billed_hours"] for r in recon)
    tot_alloc = sum(r["allocated_hours"] for r in recon)
    days_obs = {r["day"]: r["observed_hours"] for r in recon}
    tot_obs = sum(days_obs.values())
    unattr = sum(r["unattributed_cost"] for r in recon)
    log(f"  attribution: {tot_alloc:.2f}h of {tot_billed:.2f}h billed priced to spaces "
        f"({100 * tot_alloc / tot_billed if tot_billed else 0:.1f}%), "
        f"${unattr:,.2f} unattributed")
    log(f"  observed vs billed hours: {tot_obs:.2f}h / {tot_billed:.2f}h = "
        f"{100 * tot_obs / tot_billed if tot_billed else 0:.1f}%  (the method's error bar)")
    return space_days, recon, notes


# --------------------------------------------------------------------------- #
# Stage 6 -- which files were open, and for how much of the space's activity
# --------------------------------------------------------------------------- #

def fetch_cell_executions(aws: Aws, start: dt.datetime, end: dt.datetime,
                          log_floor: dt.datetime | None = None) -> tuple[list[dict], dict]:
    """Real cell executions per (space, day, notebook), from Studio telemetry.

    `sagemaker_jupyter_server_extension.telemetry_handler` emits a
    `jl-cell-executed` event for every cell run, carrying `notebook_name` --
    which is `md5(<project-relative path>)`, so it reverses against a path
    inventory (see resolve_execution_hashes).

    This measures that a notebook *ran*. The editor-sync signal it replaces only
    measured that a notebook was *open*.

    > Every event is logged twice by two formatters inside the same stream
    > (measured: 514 raw vs 257 distinct, exactly 2x on every row checked), so
    > the count MUST come from count_distinct on the event's own inner
    > timestamp. count(*) doubles it.
    """
    query = f"""
fields @logStream, @message
| filter ({APP_STREAM_FILTER}) and @message like /jl-cell-executed/
| parse @message /'timestamp': (?<ets>\\d+)/
| parse @message /"notebook_name":"(?<nbhash>[0-9a-f]{{32}})"/
| filter ispresent(nbhash) and ispresent(ets)
| stats count_distinct(ets) as execs by @logStream, nbhash, bin(1d) as day
| limit 10000
"""
    rows: list[dict] = []
    for chunk_start, chunk_end in month_chunks(log_floor or start, end):
        got = insights_adaptive(aws, STUDIO_LOG_GROUP, query, chunk_start, chunk_end)
        log(f"    {day_str(chunk_start)}: {len(got)} (space, day, notebook) exec rows")
        rows.extend(got)

    out = []
    for r in rows:
        stream, nbhash, day = r.get("@logStream"), r.get("nbhash"), r.get("day")
        if not (stream and nbhash and day):
            continue
        out.append({"space_key": stream_to_space_key(stream.lower()),
                    "day": day_str(to_utc(day)),
                    "hash": nbhash,
                    "execs": int(float(r.get("execs") or 0))})
    total = sum(r["execs"] for r in out)
    info = {"rows": len(out), "executions": total,
            "hashes": len({r["hash"] for r in out}),
            "spaces": len({r["space_key"] for r in out})}
    log(f"  cell executions: {total:,} across {info['hashes']} notebooks "
        f"in {info['spaces']} space(s)")
    return out, info


def resolve_execution_hashes(execs: list[dict], paths: set[str],
                             s3index: dict) -> tuple[dict, dict]:
    """Reverse the telemetry `notebook_name` hash back to a path.

    It is `md5` of the project-relative path exactly as the editor logs it --
    not the basename, not lowercased, not absolute. Measured on the account this
    was validated against: 150 of 155 hashes and 99.8% of executions resolve.

    Anything unresolved keeps its executions under a synthetic
    `unidentified notebook <hash8>` path, so its cost is reported rather than
    silently dropped.
    """
    inventory = set(paths)
    for cands in (s3index.get("index") or {}).values():
        for c in cands:
            parts = c["key"].split("/")
            # dzd-<domain>/<project>/<project-relative path...>
            if len(parts) > 2 and parts[0].startswith("dzd-"):
                inventory.add("/".join(parts[2:]))
            inventory.add(c["key"])

    rev: dict[str, str] = {}
    for path in inventory:
        if not path:
            continue
        for variant in (path, "/" + path):
            rev.setdefault(hashlib.md5(variant.encode()).hexdigest(), path)

    seen = {r["hash"] for r in execs}
    resolved = {h: rev[h] for h in seen if h in rev}
    unresolved = sorted(seen - set(resolved))
    ex_res = sum(r["execs"] for r in execs if r["hash"] in resolved)
    ex_tot = sum(r["execs"] for r in execs)
    info = {
        "inventory": len(inventory),
        "hashes": len(seen),
        "resolved_hashes": len(resolved),
        "unresolved_hashes": len(unresolved),
        "executions": ex_tot,
        "executions_resolved": ex_res,
        "hash_rate": (len(resolved) / len(seen)) if seen else None,
        "exec_rate": (ex_res / ex_tot) if ex_tot else None,
        "unresolved_examples": unresolved[:10],
    }
    if seen:
        log(f"  hash resolution: {len(resolved)}/{len(seen)} notebooks "
            f"({100 * len(resolved) / len(seen):.0f}%), "
            f"{ex_res:,}/{ex_tot:,} executions ({100 * ex_res / ex_tot if ex_tot else 0:.1f}%)")
        if unresolved:
            log(f"    {len(unresolved)} unidentified hash(es) keeping "
                f"{ex_tot - ex_res:,} executions -- reported, not dropped")
    return resolved, info


def normalise_doc_path(raw: str | None) -> str:
    """Jupyter writes sidecar variants of the same document. Counting
    `x.ipynb.invalid` separately from `x.ipynb` splits one notebook's activity
    across two rows and weakens the very signal the split relies on."""
    path = (raw or "").strip().strip("'\"")
    for suffix in (".invalid", ".orig", ".bak", "~"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return path


def fetch_notebook_activity(aws: Aws, start: dt.datetime, end: dt.datetime,
                            log_floor: dt.datetime | None = None) -> list[dict]:
    """Per (space, day, file) activity counts from the Jupyter server log.

    Two message shapes carry a path: the YDoc extension's file watcher, and the
    room-loaded line. Both are collaborative-editor events, so this measures
    *editing/sync activity*, not kernel execution -- which is exactly why the
    per-notebook dollar figure is an allocation and is labelled as one.
    """
    queries = [
        ("watch", r"""
fields @logStream, @timestamp, @message
| filter {f} and @message like /Watching file:/
| parse @message "Watching file: *" as nbpath
| filter ispresent(nbpath)
| stats count(*) as activity by @logStream, nbpath, bin(1d) as day
| limit 10000
""".replace("{f}", APP_STREAM_FILTER)),
        ("room", r"""
fields @logStream, @timestamp, @message
| filter {f} and @message like /loaded from file/
| parse @message "loaded from file *" as nbpath
| filter ispresent(nbpath)
| stats count(*) as activity by @logStream, nbpath, bin(1d) as day
| limit 10000
""".replace("{f}", APP_STREAM_FILTER)),
    ]
    merged: dict[tuple[str, str, str], int] = collections.Counter()
    for label, query in queries:
        rows: list[dict] = []
        for chunk_start, chunk_end in month_chunks(log_floor or start, end):
            rows.extend(insights_adaptive(aws, STUDIO_LOG_GROUP, query, chunk_start, chunk_end))
        log(f"    notebook activity [{label}]: {len(rows)} rows")
        for r in rows:
            path = normalise_doc_path(r.get("nbpath"))
            stream, day = r.get("@logStream"), r.get("day")
            if not path or not stream or not day:
                continue
            key = (stream_to_space_key(stream.lower()), day_str(to_utc(day)), path)
            merged[key] += int(float(r.get("activity") or 0))

    out = [{"space_key": sk, "day": day, "path": path, "activity": n}
           for (sk, day, path), n in merged.items()]
    log(f"  notebook activity: {len(out)} (space, day, file) rows, "
        f"{len({r['path'] for r in out})} distinct files")
    return out


def allocate_notebooks(space_days: list[dict], activity: list[dict],
                       execs: list[dict], resolved: dict,
                       profiles: dict, median_sec_per_cell: float | None) -> list[dict]:
    """Split each space-day's exact cost across the files that ran in it.

    The space-day cost itself is exact. Only the split within it is inferred,
    and it is inferred from the strongest signal available for that space-day:

      1. exec-seconds  executions x the notebook's own sec/cell. Corrects the
                       count bias -- 19 cells at 144 s/cell outweigh 139 cells
                       at 1.5 s/cell, which raw counts get backwards.
      2. exec-count    executions x the corpus median sec/cell, when the
                       notebook has no usable profile.
      3. editor-sync   editor open/sync events. Only measures that a file was
                       open. Used where no telemetry exists for that space-day.

    A space-day never mixes tiers: if it has executions, editor-sync events are
    ignored for it entirely, because "open" would otherwise dilute "ran".
    """
    cost_by_sd: dict[tuple[str, str], float] = collections.Counter()
    hours_by_sd: dict[tuple[str, str], float] = collections.Counter()
    for sd in space_days:
        cost_by_sd[(sd["space_key"], sd["day"])] += sd["cost"]
        hours_by_sd[(sd["space_key"], sd["day"])] += sd["hours"]

    fallback = median_sec_per_cell or DEFAULT_SEC_PER_CELL

    # --- tier 1/2 candidates: real executions
    exec_by_sd: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for e in execs:
        key = (e["space_key"], e["day"])
        path = resolved.get(e["hash"])
        if path:
            prof = profiles.get(path) or {}
            spc = prof.get("sec_per_cell")
            timed = prof.get("timed_cells") or 0
            if spc and spc >= MIN_SEC_PER_CELL and timed >= MIN_TIMED_CELLS:
                tier = TIER_EXEC_SECONDS
            else:
                spc, tier = fallback, TIER_EXEC_COUNT
        else:
            path = f"unidentified notebook {e['hash'][:8]}"
            spc, tier = fallback, TIER_EXEC_COUNT
        exec_by_sd[key].append({
            "path": path, "execs": e["execs"], "sec_per_cell": spc,
            "weight": e["execs"] * spc, "tier": tier,
            "identified": bool(resolved.get(e["hash"])),
            "hash": e["hash"],
        })

    # --- tier 3 candidates: editor sync
    edit_by_sd: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for a in activity:
        edit_by_sd[(a["space_key"], a["day"])].append(a)

    # Two ways a split can carry no information: too little signal to form a
    # ratio, or a spread so even that the ratio is indistinguishable from
    # dividing the day equally. Both get flagged so an even split is never
    # mistaken for a measurement.
    LOW_EXECS = 8
    LOW_ACTIVITY = 40
    FLAT_SPREAD = 0.15

    rows: list[dict] = []
    tier_cost: dict[str, float] = collections.Counter()

    for key, cost in cost_by_sd.items():
        if cost <= 0:
            continue
        hours = hours_by_sd.get(key, 0.0)
        items = exec_by_sd.get(key)
        if items:
            weights = [i["weight"] for i in items]
            total_w = sum(weights) or 1.0
            total_execs = sum(i["execs"] for i in items)
            mean = total_w / len(weights)
            spread = (statistics.pstdev(weights) / mean) if mean and len(weights) > 1 else 0.0
            weak = total_execs < LOW_EXECS or (len(weights) > 1 and spread < FLAT_SPREAD)
            for i in items:
                share = i["weight"] / total_w
                rows.append({
                    "day": key[1], "space_key": key[0], "path": i["path"],
                    "tier": i["tier"], "execs": i["execs"],
                    "sec_per_cell": round(i["sec_per_cell"], 4),
                    "kernel_hours": round(i["weight"] / 3600.0, 5),
                    "activity": None,
                    "share": round(share, 6),
                    "alloc_cost": round(cost * share, 6),
                    "alloc_hours": round(hours * share, 4),
                    "low_confidence": weak,
                    "identified": i["identified"],
                    "files_that_day": len(items),
                })
                tier_cost[i["tier"]] += cost * share
            continue

        items = edit_by_sd.get(key)
        if not items:
            continue
        counts = [i["activity"] for i in items]
        total = sum(counts) or 1
        mean = total / len(counts)
        spread = (statistics.pstdev(counts) / mean) if mean and len(counts) > 1 else 0.0
        weak = total < LOW_ACTIVITY or (len(counts) > 1 and spread < FLAT_SPREAD)
        for i in items:
            share = i["activity"] / total
            rows.append({
                "day": key[1], "space_key": key[0], "path": i["path"],
                "tier": TIER_EDITOR_SYNC, "execs": None, "sec_per_cell": None,
                "kernel_hours": None,
                "activity": i["activity"],
                "share": round(share, 6),
                "alloc_cost": round(cost * share, 6),
                "alloc_hours": round(hours * share, 4),
                "low_confidence": weak,
                "identified": True,
                "files_that_day": len(counts),
            })
            tier_cost[TIER_EDITOR_SYNC] += cost * share

    placed = {(r["space_key"], r["day"]) for r in rows}
    unmatched = sum(c for k, c in cost_by_sd.items() if k not in placed)
    total = sum(r["alloc_cost"] for r in rows)
    soft = sum(r["alloc_cost"] for r in rows if r["low_confidence"])
    log(f"  notebook allocation: {len(rows)} rows; "
        f"${unmatched:,.2f} of space-day cost had no file signal at all")
    for tier in (TIER_EXEC_SECONDS, TIER_EXEC_COUNT, TIER_EDITOR_SYNC):
        if tier_cost.get(tier):
            log(f"    {tier:<13} ${tier_cost[tier]:>10,.2f}  "
                f"({100 * tier_cost[tier] / total if total else 0:.0f}%)")
    log(f"  of ${total:,.2f} allocated to files, ${soft:,.2f} "
        f"({100 * soft / total if total else 0:.0f}%) rests on a weak split")
    return rows


# --------------------------------------------------------------------------- #
# Stage 7 -- what the code actually does
# --------------------------------------------------------------------------- #

def build_s3_index(aws: Aws) -> dict:
    """basename -> [keys] over every project's `shared/` tree.

    The log path and the S3 key do not agree: folders get reorganised in the
    mirror, so `shared/A/B/x.ipynb` in the log can be
    `shared/B/A/B/x.ipynb` in S3. Match on basename, then longest common suffix.
    """
    s3 = aws.c("s3")
    index: dict[str, list[dict]] = collections.defaultdict(list)
    buckets: list[str] = []
    try:
        want = re.compile(rf"^amazon-(sagemaker|datazone)-{aws.account}-{aws.region}-")
        for b in s3.list_buckets().get("Buckets", []):
            if want.match(b["Name"]):
                buckets.append(b["Name"])
    except Exception as exc:
        aws.warn(f"list_buckets failed ({_short(exc)}); code profiling disabled")
        return {"index": {}, "buckets": []}

    total = 0
    for bucket in buckets:
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if "/.ipynb_checkpoints/" in key or key.endswith("/"):
                        continue
                    if not key.lower().endswith(CODE_FILE_SUFFIXES):
                        continue
                    index[os.path.basename(key)].append({
                        "bucket": bucket, "key": key,
                        "size": obj["Size"],
                        "last_modified": to_utc(obj["LastModified"]).isoformat(),
                        "etag": (obj.get("ETag") or "").strip('"'),
                    })
                    total += 1
        except Exception as exc:
            aws.warn(f"cannot list s3://{bucket} ({_short(exc)})")
    log(f"  s3 index: {total} code files across {len(buckets)} bucket(s)")
    return {"index": dict(index), "buckets": buckets}


def match_s3(path: str, index: dict) -> dict | None:
    base = os.path.basename(path)
    cands = index.get(base)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    want = [p for p in path.strip("/").split("/") if p]
    best, best_score = None, -1
    for c in cands:
        have = [p for p in c["key"].strip("/").split("/") if p]
        score = 0
        for a, b in zip(reversed(want), reversed(have)):
            if a != b:
                break
            score += 1
        if score > best_score:
            best, best_score = c, score
    return best


EXEC_BUSY_RE = re.compile(rb'"iopub\.status\.busy"\s*:\s*"([^"]+)"')
EXEC_IDLE_RE = re.compile(rb'"iopub\.status\.idle"\s*:\s*"([^"]+)"')


def _iso_seconds(a: str, b: str) -> float | None:
    try:
        pa, pb = to_utc(a), to_utc(b)
    except Exception:
        return None
    d = (pb - pa).total_seconds()
    return d if 0 <= d < 86400 * 2 else None


def cell_timings_from_cells(code_cells: list[dict]) -> dict:
    """Per-cell kernel time from nbformat execution metadata.

    Jupyter records `iopub.status.busy` -> `iopub.status.idle` per cell, with
    absolute UTC timestamps. 18 of 18 notebooks sampled there carried it,
    covering 97% of code cells.

    NOTE this is a snapshot of each cell's MOST RECENT run, not a history, so
    the derived sec/cell is a profile of the notebook rather than a measurement
    of any particular day. Callers must label it as such.
    """
    secs, timed, newest = 0.0, 0, None
    for c in code_cells:
        ex = ((c.get("metadata") or {}).get("execution") or {})
        busy = ex.get("iopub.status.busy") or ex.get("shell.execute_reply.started")
        idle = ex.get("iopub.status.idle") or ex.get("shell.execute_reply")
        if not (busy and idle):
            continue
        d = _iso_seconds(busy, idle)
        if d is None:
            continue
        secs += d
        timed += 1
        newest = busy if (newest is None or busy > newest) else newest
    return _timing_summary(secs, timed, newest)


def cell_timings_from_bytes(body: bytes) -> dict:
    """Same figures without parsing the JSON.

    Notebooks here reach 87 MB, nearly all of it saved output; a full
    json.loads costs about a gigabyte for fields we throw away. The busy/idle
    pairs appear in document order, so a byte scan recovers them.
    """
    busy = EXEC_BUSY_RE.findall(body)
    idle = EXEC_IDLE_RE.findall(body)
    secs, timed, newest = 0.0, 0, None
    for b, i in zip(busy, idle):
        bs, is_ = b.decode("utf-8", "replace"), i.decode("utf-8", "replace")
        d = _iso_seconds(bs, is_)
        if d is None:
            continue
        secs += d
        timed += 1
        newest = bs if (newest is None or bs > newest) else newest
    return _timing_summary(secs, timed, newest)


def _timing_summary(secs: float, timed: int, newest: str | None) -> dict:
    return {
        "timed_cells": timed,
        "kernel_seconds": round(secs, 3),
        "sec_per_cell": round(secs / timed, 4) if timed else None,
        "profile_newest": newest,
    }


def profile_notebook(aws: Aws, meta: dict, cache_dir: Path | None) -> dict:
    cache_file = None
    if cache_dir and meta.get("etag"):
        cache_file = cache_dir / f"nb_v{PROFILE_SCHEMA}_{meta['etag']}.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                if cached.get("schema") == PROFILE_SCHEMA:
                    return cached
            except Exception:
                pass

    out = {"schema": PROFILE_SCHEMA, "s3_key": meta["key"], "bucket": meta["bucket"],
           "size": meta["size"], "last_modified": meta["last_modified"], "parsed": False}
    if meta["size"] > NB_MAX_BYTES:
        out["error"] = f"skipped: {meta['size'] / 1e6:.0f} MB exceeds the parse cap"
        return out
    try:
        body = aws.c("s3").get_object(Bucket=meta["bucket"], Key=meta["key"])["Body"].read()
    except Exception as exc:
        out["error"] = _short(exc)
        return out

    try:
        if meta["key"].lower().endswith(".ipynb"):
            if meta["size"] >= FULL_PARSE_BYTES:
                # Timings only: they are what the cost allocation depends on.
                out.update(cell_timings_from_bytes(body))
                out["cells"] = out["code_cells"] = None
                out["kernel"] = out["max_execution_count"] = None
                out["imports"] = out["libs"] = out["heavy"] = out["tables"] = []
                out["partial"] = (f"timings only: {meta['size'] / 1e6:.0f} MB is above the "
                                  f"{FULL_PARSE_BYTES / 1e6:.0f} MB full-parse threshold")
                out["parsed"] = True
                if cache_file:
                    try:
                        cache_file.write_text(json.dumps(out))
                    except Exception:
                        pass
                return out
            nb = json.loads(body)
            cells = nb.get("cells") or []
            code_cells = [c for c in cells if c.get("cell_type") == "code"]
            src = "\n".join("".join(c.get("source") or []) for c in code_cells)
            out["kernel"] = ((nb.get("metadata") or {}).get("kernelspec") or {}).get("display_name")
            out["cells"] = len(cells)
            out["code_cells"] = len(code_cells)
            out["max_execution_count"] = max(
                [c.get("execution_count") or 0 for c in code_cells], default=0)
            out.update(cell_timings_from_cells(code_cells))
        else:
            src = body.decode("utf-8", "replace")
            out["cells"] = out["code_cells"] = None
            out["kernel"] = None
            out["max_execution_count"] = None
        out["source_chars"] = len(src)
        imports = set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z0-9_]+)", src, re.M))
        out["imports"] = sorted(imports)[:40]
        low = src.lower()
        out["libs"] = [h for h in CODE_LIB_HINTS if h.lower() in low]
        out["heavy"] = [h for h in CODE_HEAVY_HINTS if h.lower() in low]
        tables = set(re.findall(
            r"(?i)\b(?:from|join|insert\s+into|update)\s+([a-z_][a-z0-9_]*\.[a-z0-9_]+)", src))
        noise = re.compile(r"^(sklearn|scipy|numpy|pandas|optuna|torch|matplotlib|plotly|"
                           r"statsmodels|xgboost|lightgbm|catboost|os|sys|json|datetime|re|"
                           r"boto3|typing|collections|dateutil|IPython|seaborn|pyspark|dask)\.")
        out["tables"] = sorted(t for t in tables if not noise.match(t))[:25]
        out["parsed"] = True
    except Exception as exc:
        out["error"] = f"parse failed: {_short(exc)}"

    if cache_file:
        try:
            cache_file.write_text(json.dumps(out))
        except Exception:
            pass
    return out


def profile_executed_notebooks(aws: Aws, paths: list[str], s3index: dict,
                               top_n: int, cache_dir: Path | None,
                               workers: int) -> tuple[dict, float | None]:
    """Profile the notebooks that actually ran, ranked by execution count.

    Profiling used to run after allocation, ranked by cost. The kernel-second
    weighting inverts that: allocation needs sec/cell, so profiling has to come
    first, and the only ranking available beforehand is execution count. That
    carries a mild bias -- a rarely-run but very slow notebook could fall
    outside the cap -- which the generous default and the ETag cache make
    academic, but it is a bias, not an exactness.

    Returns the profiles and the corpus median sec/cell, which is the fallback
    weight for notebooks with no usable profile.
    """
    if not s3index.get("index"):
        return {}, None
    matched = {}
    for path in paths[:top_n]:
        m = match_s3(path, s3index["index"])
        if m:
            matched[path] = m

    profiles: dict[str, dict] = {}
    if matched:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(profile_notebook, aws, meta, cache_dir): path
                    for path, meta in matched.items()}
            for fut in as_completed(futs):
                profiles[futs[fut]] = fut.result()

    per_cell = [p["sec_per_cell"] for p in profiles.values()
                if p.get("sec_per_cell") and p["sec_per_cell"] >= MIN_SEC_PER_CELL
                and (p.get("timed_cells") or 0) >= MIN_TIMED_CELLS]
    median = statistics.median(per_cell) if per_cell else None
    timed = sum(1 for p in profiles.values() if p.get("timed_cells"))
    log(f"  code profiles: {len(profiles)}/{min(len(paths), top_n)} ranked files matched "
        f"in S3; {timed} carry per-cell timings")
    if median:
        log(f"  corpus median: {median:.2f} sec/cell "
            f"(fallback for notebooks without a profile)")
    return profiles, median


# --------------------------------------------------------------------------- #
# Payer-account context
# --------------------------------------------------------------------------- #

def fetch_payer_context(aws: Aws) -> dict:
    """Tag activation status, CUR presence, resource-level opt-in.

    Everything here is optional: each probe degrades to a note instead of
    aborting, because a report that cannot reach the payer is still a valid
    report -- it just has to say so rather than imply the gap is a finding.
    """
    ctx = {"available": False, "account": aws.payer_account, "mode": aws.payer_mode,
           "tags": {}, "tags_active": [], "tag_total": 0, "cur": [], "exports": [],
           "resource_level": False, "notes": []}
    if not aws.payer_mode:
        ctx["notes"].append(
            "No usable payer profile, so cost-allocation tag status, CUR availability and "
            "resource-level granularity are unknown. They are payer-only settings; a member "
            "account cannot read them at any permission level.")
        return ctx
    ctx["available"] = True

    def call(service_args: list[str], boto_fn, region="us-east-1"):
        """Prefer boto3; fall back to the CLI for profiles boto3 cannot load."""
        if aws.payer_mode == "boto3":
            try:
                return boto_fn()
            except Exception as exc:
                ctx["notes"].append(f"{' '.join(service_args)} failed: {_short(exc)}")
                return None
        out = cli_json(aws.payer_profile, service_args, region)
        if out is None:
            ctx["notes"].append(f"aws {' '.join(service_args)} could not be run")
            return None
        if "__error__" in out:
            ctx["notes"].append(f"aws {' '.join(service_args)}: {out['__error__']}")
            return None
        return out

    def ce_client():
        return aws.payer_session.client("ce", region_name="us-east-1")

    def all_tags_boto():
        tok, tags = None, []
        cl = ce_client()
        while True:
            kw = {"MaxResults": 100}
            if tok:
                kw["NextToken"] = tok
            r = cl.list_cost_allocation_tags(**kw)
            tags.extend(r.get("CostAllocationTags", []))
            tok = r.get("NextToken")
            if not tok:
                return {"CostAllocationTags": tags}

    tags_resp = call(["ce", "list-cost-allocation-tags"], all_tags_boto)
    if tags_resp:
        tags = tags_resp.get("CostAllocationTags", [])
        ctx["tag_total"] = len(tags)
        ctx["tags"] = {t["TagKey"]: t.get("Status") for t in tags
                       if t["TagKey"] in DATAZONE_TAG_KEYS}
        for key in DATAZONE_TAG_KEYS:
            ctx["tags"].setdefault(key, None)
        ctx["tags_active"] = sorted(t["TagKey"] for t in tags if t.get("Status") == "Active")

    cur_resp = call(["cur", "describe-report-definitions"],
                    lambda: aws.payer_session.client(
                        "cur", region_name="us-east-1").describe_report_definitions())
    if cur_resp:
        ctx["cur"] = [d.get("ReportName") for d in cur_resp.get("ReportDefinitions", [])]

    exp_resp = call(["bcm-data-exports", "list-exports"],
                    lambda: aws.payer_session.client(
                        "bcm-data-exports", region_name="us-east-1").list_exports())
    if exp_resp:
        ctx["exports"] = [e.get("ExportName") or e.get("ExportArn")
                          for e in exp_resp.get("Exports", [])]

    # Resource-level granularity is an opt-in; probing it is the only way to
    # find out, and a denial here is the expected answer, not an error.
    today = dt.datetime.now(dt.timezone.utc)
    period = {"Start": day_str(today - dt.timedelta(days=2)), "End": day_str(today)}
    flt = {"Dimensions": {"Key": "SERVICE", "Values": [SERVICE]}}
    if aws.payer_mode == "boto3":
        try:
            ce_client().get_cost_and_usage_with_resources(
                TimePeriod=period, Granularity="DAILY", Metrics=["UnblendedCost"],
                Filter=flt, GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"}])
            ctx["resource_level"] = True
        except Exception:
            ctx["resource_level"] = False
    else:
        probe = cli_json(aws.payer_profile, ["ce", "get-cost-and-usage-with-resources"],
                         "us-east-1",
                         ["--time-period", f"Start={period['Start']},End={period['End']}",
                          "--granularity", "DAILY", "--metrics", "UnblendedCost",
                          "--filter", json.dumps(flt),
                          "--group-by", "Type=DIMENSION,Key=RESOURCE_ID"])
        ctx["resource_level"] = bool(probe and "__error__" not in probe)

    log(f"  payer {ctx['account']} via {ctx['mode']}: {ctx['tag_total']} tags "
        f"({len(ctx['tags_active'])} active), CUR={len(ctx['cur'])}, "
        f"exports={len(ctx['exports'])}, resource-level={ctx['resource_level']}")
    return ctx


def build_recommendations(payer: dict, ct_info: dict, observed_coverage: float | None,
                          priced_coverage: float | None = None) -> list[dict]:
    recs = []
    inactive = [k for k in DATAZONE_TAG_KEYS if payer.get("tags", {}).get(k) != "Active"]
    if payer["available"] and inactive:
        recs.append({
            "severity": "high",
            "title": "Activate the DataZone cost-allocation tags",
            "body": "SageMaker Unified Studio already stamps " + ", ".join(inactive) +
                    " on every Studio app, but they are inactive for billing, so Cost "
                    "Explorer cannot group by project or user. Activating them in the payer "
                    "account (Billing > Cost allocation tags) makes per-project and per-user "
                    "cost exact within ~24h. It is NOT retroactive -- past periods will "
                    "always need the reconstruction this report performs.",
        })
    if payer["available"] and not (payer["cur"] or payer["exports"]):
        recs.append({
            "severity": "high",
            "title": "Enable a Cost and Usage Report with resource IDs",
            "body": "No CUR or data export exists in the payer account. A CUR including "
                    "line_item_resource_id delivers the Studio app ARN with its hourly "
                    "billed amount, which would make per-space cost exact and directly "
                    "sourced instead of reconstructed.",
        })
    if payer["available"] and not payer["resource_level"]:
        recs.append({
            "severity": "medium",
            "title": "Turn on resource-level Cost Explorer granularity",
            "body": "Resource-level data is an opt-in in the payer account's Cost Explorer "
                    "settings. Once enabled it exposes per-app ARNs for a trailing 14-day "
                    "window -- enough to cross-check this report continuously.",
        })
    if not ct_info.get("trail_archive"):
        recs.append({
            "severity": "medium",
            "title": "Create an organization CloudTrail with an S3 archive",
            "body": "No trail and no event data store exist, so CloudTrail history stops at "
                    f"{CLOUDTRAIL_LOOKBACK_DAYS} days ({ct_info.get('from')}). Everything "
                    "older is reconstructed from log occupancy, which cannot see the instance "
                    "type. A trail permanently removes that gap.",
        })
    if observed_coverage is not None and observed_coverage > 1.12:
        recs.append({
            "severity": "low",
            "title": f"Observed runtime is {observed_coverage:.1%} of billed runtime",
            "body": "Above roughly 106% this exceeds the known provisioning offset and "
                    "occupancy bin-tail overshoot, which means something is being "
                    "double-counted. Check for case-variant space names appearing as two "
                    "log streams, and for CloudTrail spans left open by a failed sweep.",
        })
    if priced_coverage is not None and priced_coverage < 0.98:
        recs.append({
            "severity": "high",
            "title": f"Only {priced_coverage:.1%} of billed hours could be priced to a space",
            "body": "The rest is real spend with no space activity in CloudTrail or the "
                    "Studio log group. The usual cause is an app type this tool does not "
                    "reconstruct, or a domain whose logs were never delivered. Check the "
                    "reconciliation table for the days carrying the shortfall.",
        })
    return recs


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #

def collect(args) -> dict:
    aws = Aws(args.profile, args.region, args.payer_profile)
    start, end = args.start, args.end
    cache_dir = None
    if args.cache:
        cache_dir = Path(args.cache)
        cache_dir.mkdir(parents=True, exist_ok=True)

    log(f"account {aws.account} / {aws.region} / {day_str(start)}..{day_str(end)}")
    log("stage 1  billed truth (Cost Explorer)")
    billed = fetch_billed(aws, start, end)
    if not billed:
        log("  no SageMaker spend in this period")

    log("stage 2  identity (SageMaker + DataZone)")
    identity = fetch_identity(aws)

    log("stage 3a cloudtrail app spans")
    ct_spans, ct_info = fetch_cloudtrail_spans(aws, start, end, args.workers)

    log("stage 3b log occupancy (Logs Insights)")
    occupancy, occ_info = fetch_log_occupancy(aws, start, end, args.bin_minutes, args.gap_bins)

    clip_open_spans(ct_spans, occ_info["sessions"], end)

    log("stage 4/5 attribute billed hours to spaces, then price them")
    space_days, recon, type_notes = attribute_days(
        billed, ct_spans, occupancy, ct_info.get("complete", True))

    log_floor = None
    if occ_info.get("window_start"):
        log_floor = to_utc(occ_info["window_start"] + "T00:00:00+00:00")

    # Stage 6 runs in dependency order, not in the order the report reads:
    # the kernel-second weighting needs each notebook's sec/cell, so the S3
    # profiles have to exist before the allocation can be computed.
    activity, execs, exec_info = [], [], {}
    resolved, resolve_info, profiles = {}, {}, {}
    median_spc = None
    nb_rows: list[dict] = []

    if not args.no_notebooks:
        log("stage 6a cell executions (telemetry)")
        execs, exec_info = fetch_cell_executions(aws, start, end, log_floor)

        log("stage 6b file paths (editor events)")
        activity = fetch_notebook_activity(aws, start, end, log_floor)
        editor_paths = {a["path"] for a in activity}

        s3index = {"index": {}, "buckets": []}
        if not args.no_code:
            log("stage 6c S3 notebook index")
            s3index = build_s3_index(aws)

        resolved, resolve_info = resolve_execution_hashes(execs, editor_paths, s3index)

        if not args.no_code and resolved:
            log("stage 6d code profiles + cell timings (S3)")
            ranked = collections.Counter()
            for e in execs:
                path = resolved.get(e["hash"])
                if path:
                    ranked[path] += e["execs"]
            # Any file that ran but never resolved still gets profiled if the
            # editor saw it, so the ranking is not the only route into S3.
            order = [p for p, _ in ranked.most_common()]
            order += sorted(editor_paths - set(order))
            profiles, median_spc = profile_executed_notebooks(
                aws, order, s3index, args.top_notebooks, cache_dir, args.workers)

        log("stage 6e allocate space-day cost across files")
        nb_rows = allocate_notebooks(space_days, activity, execs, resolved,
                                     profiles, median_spc)

    log("payer context")
    payer = fetch_payer_context(aws)

    # ---- space metadata, including spaces that no longer exist
    seen = {sd["space_key"] for sd in space_days} | {sp["space_key"] for sp in ct_spans}
    ct_tags: dict[str, dict] = {}
    for sp in ct_spans:
        if sp.get("tags"):
            ct_tags.setdefault(sp["space_key"], sp["tags"])

    spaces_meta = {}
    for sk in sorted(seen):
        base = identity["spaces"].get(sk, {})
        tags = ct_tags.get(sk, {})
        domain_id = base.get("domain_id") or sk.split("/")[0]
        owner_uuid = base.get("owner_uuid") or tags.get("AmazonDataZoneUser")
        project_id = tags.get("AmazonDataZoneProject")
        # Space names of the form default-<uuid> encode their owner.
        if not owner_uuid:
            m = re.match(r"^default-([0-9a-f\-]{36})$", sk.split("/", 1)[-1])
            if m:
                owner_uuid = m.group(1)
        # SMUS domain names embed the DataZone project id:
        # SageMakerUnifiedStudio-<projectId>-<envId>-<stage>. That is the only
        # way to recover the project for periods with no CloudTrail tags.
        if not project_id:
            dm = re.match(r"^SageMakerUnifiedStudio-([a-z0-9]+)-",
                          identity["domains"].get(domain_id, "") or "")
            if dm and dm.group(1) in identity["projects"]:
                project_id = dm.group(1)
        spaces_meta[sk] = {
            "space_key": sk,
            "domain_id": domain_id,
            "domain_name": identity["domains"].get(domain_id, domain_id),
            "space": sk.split("/", 1)[-1],
            "app_type": base.get("app_type") or "JupyterLab",
            "owner_uuid": owner_uuid,
            "owner": identity["users"].get(owner_uuid) if owner_uuid else None,
            "project_id": project_id,
            "project": identity["projects"].get(project_id) if project_id else None,
            "ebs_gb": base.get("ebs_gb"),
            "exists": bool(base.get("exists")),
        }

    storage = [r for r in billed if is_storage_kind(r["kind"])]
    other = [r for r in billed if not is_storage_kind(r["kind"]) and not
             (is_app_kind(r["kind"]) and r["instance"])]

    # Two different numbers, deliberately kept apart:
    #   priced_coverage  = billed hours actually priced to a space. Shortfalls
    #                      here are real spend nobody can be charged for.
    #   observed_coverage = observed runtime against billed runtime. This is the
    #                      method's error bar and sits slightly above 100%.
    tot_billed_h = sum(r["billed_hours"] for r in recon)
    tot_alloc_h = sum(r["allocated_hours"] for r in recon)
    obs_per_day = {}
    for r in recon:
        obs_per_day[r["day"]] = max(obs_per_day.get(r["day"], 0.0), r["observed_hours"])
    tot_obs_h = sum(obs_per_day.values())
    priced_coverage = (tot_alloc_h / tot_billed_h) if tot_billed_h else None
    observed_coverage = (tot_obs_h / tot_billed_h) if tot_billed_h else None

    unvalidated = sorted({r["kind"] for r in billed
                          if r["kind"] in ATTRIB_BY_KIND
                          and not ATTRIB_BY_KIND[r["kind"]]["validated"]})

    sources_used = ["cost-explorer", "sagemaker-api"]
    if identity["users"]:
        sources_used.append("datazone")
    if ct_info.get("available"):
        sources_used.append("cloudtrail-event-history")
    if occ_info["streams"]:
        sources_used.append("cloudwatch-logs-insights")
    if profiles:
        sources_used.append("s3-notebooks")
    if payer["available"]:
        sources_used.append("payer-cost-explorer")

    return {
        "meta": {
            "account": aws.account, "region": aws.region, "profile": args.profile,
            "payer_profile": args.payer_profile, "payer_account": aws.payer_account,
            "start": day_str(start), "end": day_str(end),
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "sources_used": sources_used,
            "unvalidated_kinds": unvalidated,
            "warnings": aws.warnings,
            "cloudtrail": ct_info,
            "occupancy": {k: v for k, v in occ_info.items() if k != "sessions"},
            "priced_coverage": priced_coverage,
            "observed_coverage": observed_coverage,
            "billed_hours": round(tot_billed_h, 3),
            "allocated_hours": round(tot_alloc_h, 3),
            "observed_hours": round(tot_obs_h, 3),
            "total_cost": round(sum(r["cost"] for r in billed), 4),
            "runtime_cost": round(sum(r["billed_cost"] for r in recon), 4),
            "storage_cost": round(sum(r["cost"] for r in storage), 4),
            "other_cost": round(sum(r["cost"] for r in other), 4),
            "unattributed_cost": round(sum(r["unattributed_cost"] for r in recon), 4),
            "type_notes": type_notes,
            "resource_map": RESOURCE_MAP,
            "file_alloc_cost": round(sum(r["alloc_cost"] for r in nb_rows), 4),
            "file_alloc_soft_cost": round(
                sum(r["alloc_cost"] for r in nb_rows if r["low_confidence"]), 4),
            "executions": exec_info,
            "hash_resolution": resolve_info,
            "median_sec_per_cell": median_spc,
            "weight_tiers": {
                t: round(sum(r["alloc_cost"] for r in nb_rows if r["tier"] == t), 4)
                for t in (TIER_EXEC_SECONDS, TIER_EXEC_COUNT, TIER_EDITOR_SYNC)
            },
        },
        "payer": payer,
        "recommendations": build_recommendations(payer, ct_info, observed_coverage,
                                             priced_coverage),
        "spaces": spaces_meta,
        "space_days": space_days,
        "recon": recon,
        "notebooks": nb_rows,
        "profiles": profiles,
        "spans": [{"space_key": sp["space_key"], "start": sp["start"].isoformat(),
                   "end": sp["end"].isoformat(), "instance": sp["instance"],
                   "closed": sp.get("closed"), "end_source": sp.get("end_source"),
                   "hours": round((sp["end"] - sp["start"]).total_seconds() / 3600.0, 4),
                   "app_type": sp.get("app_type"), "actor": sp.get("actor")}
                  for sp in sorted(ct_spans, key=lambda x: x["start"])],
        "storage": [{"day": r["day"], "usage_type": r["usage_type"],
                     "cost": r["cost"], "qty": r["qty"]} for r in storage],
        "other": [{"day": r["day"], "usage_type": r["usage_type"],
                   "kind": r["kind"], "cost": r["cost"], "qty": r["qty"]} for r in other],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_period(args) -> tuple[dt.datetime, dt.datetime]:
    def d(s):
        return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    if args.start and args.end:
        return d(args.start), d(args.end)
    today = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = today.replace(day=1)
    start = end
    for _ in range(args.months):
        start = (start - dt.timedelta(days=1)).replace(day=1)
    return start, end


def default_out(meta_start: dt.datetime, meta_end: dt.datetime) -> Path:
    return Path.cwd() / (f"sagemaker_cost_deepdive_"
                         f"{meta_start:%Y-%m-%d}_{meta_end:%Y-%m-%d}.html")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SageMaker cost deep dive -> self-contained HTML dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Dollars are shares of the billed amount and always reconcile. "
               "Hours coverage is the attribution-quality metric and typically "
               "lands slightly above 100% -- see references/attribution-method.md.")
    ap.add_argument("--profile", required=True, help="workload AWS profile")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--payer-profile", default=None,
                    help="organization payer profile; enables cost-allocation tag and CUR "
                         "checks. Degrades to workload-only mode if absent or expired.")
    ap.add_argument("--start", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", help="YYYY-MM-DD (exclusive)")
    ap.add_argument("--months", type=int, default=3,
                    help="whole months back from the start of this month (default 3)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json-out", default=None, help="also write the raw collected data")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    ap.add_argument("--no-code", action="store_true", help="skip the S3 notebook profiling")
    ap.add_argument("--no-notebooks", action="store_true",
                    help="skip notebook attribution entirely (space level only)")
    ap.add_argument("--top-notebooks", type=int, default=150,
                    help="how many files to profile from S3, ranked by execution count "
                         "(default 150 -- enough to cover a full year here)")
    ap.add_argument("--bin-minutes", type=int, default=5)
    ap.add_argument("--gap-bins", type=int, default=3,
                    help="quiet bins tolerated inside one session (default 3)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--cache", default=".sm_cost_cache",
                    help="directory for notebook parse cache ('' to disable)")
    args = ap.parse_args()

    args.start, args.end = parse_period(args)
    if args.start >= args.end:
        sys.exit("--start must be before --end")

    t0 = time.time()
    data = collect(args)

    out = Path(args.out) if args.out else default_out(args.start, args.end)
    out.write_text(render(data), encoding="utf-8")
    log(f"\nwrote {out}  ({out.stat().st_size / 1024:.0f} KB, {time.time() - t0:.0f}s)")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(data, indent=2, default=str))
        log(f"wrote {args.json_out}")

    print_summary(data)
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def print_summary(data: dict) -> None:
    m = data["meta"]
    log("")
    log(f"  period            {m['start']} .. {m['end']}")
    log(f"  total billed      ${m['total_cost']:,.2f}"
        f"   (runtime ${m['runtime_cost']:,.2f}, storage ${m['storage_cost']:,.2f}"
        f", other ${m['other_cost']:,.2f})")
    if m["priced_coverage"] is not None:
        log(f"  priced coverage   {m['priced_coverage']:.1%}"
            f"  ({m['allocated_hours']:,.1f}h of {m['billed_hours']:,.1f}h billed priced to a space)")
        log(f"  observed/billed   {m['observed_coverage']:.1%}"
            f"  ({m['observed_hours']:,.1f}h observed) <- the method's error bar")
    log(f"  spaces            {len(data['spaces'])}")
    log(f"  files attributed  {len({r['path'] for r in data['notebooks']})}")
    ex = m.get("executions") or {}
    if ex.get("executions"):
        hr = (m.get("hash_resolution") or {})
        log(f"  cell executions   {ex['executions']:,} across {ex.get('hashes', 0)} notebooks"
            + (f"  ({hr['exec_rate']:.1%} identified)" if hr.get("exec_rate") else ""))
    tiers = m.get("weight_tiers") or {}
    if any(tiers.values()):
        tot = sum(tiers.values()) or 1
        log("  weighting         " + "  ".join(
            f"{t}=${v:,.0f} ({100 * v / tot:.0f}%)" for t, v in tiers.items() if v))
    log(f"  sources           {', '.join(m['sources_used'])}")
    if m["unattributed_cost"] > 0.005:
        log(f"  UNATTRIBUTED      ${m['unattributed_cost']:,.2f} of runtime cost had no "
            f"space activity in any source")
    for w in m["warnings"]:
        log(f"  warning           {w}")

    top = collections.Counter()
    for sd in data["space_days"]:
        top[sd["space_key"]] += sd["cost"]
    log("")
    log("  top spaces by cost")
    for sk, cost in top.most_common(8):
        meta = data["spaces"].get(sk, {})
        who = meta.get("owner") or meta.get("owner_uuid") or "unknown"
        log(f"    ${cost:>10,.2f}  {who:<18} {meta.get('project') or '-':<28} {sk}")

    nb = collections.Counter()
    for r in data["notebooks"]:
        nb[r["path"]] += r["alloc_cost"]
    if nb:
        log("")
        log("  top files by allocated cost (allocation, not measurement)")
        for path, cost in nb.most_common(10):
            prof = data["profiles"].get(path) or {}
            libs = ", ".join((prof.get("libs") or [])[:4])
            log(f"    ${cost:>10,.2f}  {path[:64]:<66} {libs}")


if __name__ == "__main__":
    sys.exit(main())
