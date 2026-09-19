#!/usr/bin/env python3
"""Per-bucket S3 storage footprint and the lifecycle gaps that quietly cost money.

READ-ONLY. Uses list_buckets, get_bucket_* and CloudWatch daily storage metrics.

Sizes come from CloudWatch's daily BucketSizeBytes metric rather than from listing
objects. That is free and instant; listing a bucket with millions of objects is neither.
The trade-off is up to 48 hours of staleness, and buckets created very recently may not
report at all.

Findings raised:
  - Buckets with no lifecycle configuration at all
  - Versioning enabled with no rule expiring noncurrent versions
  - No rule aborting incomplete multipart uploads (invisible in the console, still billed)
  - Large buckets sitting entirely in Standard storage

Examples
    s3_storage.py --profile personal
    s3_storage.py --profile personal --min-gb 10 --json
"""

import sys
from datetime import datetime

import _common as c

# Approximate us-east-1 list prices, USD per GB-month. Ranking aid, not a quote.
PRICE_PER_GB = {
    "StandardStorage": 0.023,
    "IntelligentTieringFAStorage": 0.023,
    "IntelligentTieringIAStorage": 0.0125,
    "StandardIAStorage": 0.0125,
    "OneZoneIAStorage": 0.01,
    "GlacierInstantRetrievalStorage": 0.004,
    "GlacierStorage": 0.0036,
    "DeepArchiveStorage": 0.00099,
    "ReducedRedundancyStorage": 0.024,
}

STORAGE_TYPES = list(PRICE_PER_GB)
GIB = 1024.0 ** 3


def build_args():
    parser = c.base_parser(__doc__.split("\n")[0])
    parser.add_argument(
        "--min-gb",
        type=float,
        default=1.0,
        help="Ignore buckets smaller than this many GB (default: 1).",
    )
    parser.add_argument(
        "--top", type=int, default=25, help="Buckets to list (default: 25)."
    )
    parser.add_argument(
        "--standard-only-gb",
        type=float,
        default=100.0,
        help="Flag Standard-only buckets above this size (default: 100).",
    )
    return parser.parse_args()


def bucket_regions(s3, buckets):
    """Map each bucket to its region. CloudWatch metrics are regional."""
    regions = {}
    for bucket in buckets:
        name = bucket["Name"]
        response = c.try_call(
            s3,
            "get_bucket_location",
            "s3:GetBucketLocation for %s" % name,
            Bucket=name,
        )
        if response is None:
            continue
        # us-east-1 is reported as None for historical reasons.
        regions[name] = response.get("LocationConstraint") or "us-east-1"
    return regions


def storage_by_class(cloudwatch, bucket):
    """Latest daily BucketSizeBytes per storage class, in bytes."""
    start, end = c.utc_window(3)
    sizes = {}
    for storage_type in STORAGE_TYPES:
        response = c.try_call(
            cloudwatch,
            "get_metric_statistics",
            "cloudwatch:GetMetricStatistics for %s" % bucket,
            Namespace="AWS/S3",
            MetricName="BucketSizeBytes",
            Dimensions=[
                {"Name": "BucketName", "Value": bucket},
                {"Name": "StorageType", "Value": storage_type},
            ],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Average"],
        )
        if not response:
            continue
        points = response.get("Datapoints", [])
        if not points:
            continue
        latest = max(points, key=lambda p: p["Timestamp"])
        if latest["Average"] > 0:
            sizes[storage_type] = latest["Average"]
    return sizes


def object_count(cloudwatch, bucket):
    start, end = c.utc_window(3)
    response = c.try_call(
        cloudwatch,
        "get_metric_statistics",
        "cloudwatch:GetMetricStatistics objects for %s" % bucket,
        Namespace="AWS/S3",
        MetricName="NumberOfObjects",
        Dimensions=[
            {"Name": "BucketName", "Value": bucket},
            {"Name": "StorageType", "Value": "AllStorageTypes"},
        ],
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Average"],
    )
    if not response:
        return None
    points = response.get("Datapoints", [])
    if not points:
        return None
    return int(max(points, key=lambda p: p["Timestamp"])["Average"])


def lifecycle_rules(s3, bucket):
    """Lifecycle rules, [] when the bucket has no configuration, None when unreadable.

    The three outcomes must stay distinct: [] is a finding, None is an unknown. A
    bucket whose policy we could not read must not be reported as having no policy.
    """
    response = c.try_call(
        s3,
        "get_bucket_lifecycle_configuration",
        "s3:GetBucketLifecycleConfiguration for %s" % bucket,
        expected=("NoSuchLifecycleConfiguration", "NoSuchLifecycleConfigurationError"),
        Bucket=bucket,
    )
    if response is c.EXPECTED:
        return []
    if response is None:
        return None
    return response.get("Rules", [])


def versioning_state(s3, bucket):
    response = c.try_call(
        s3, "get_bucket_versioning", "s3:GetBucketVersioning for %s" % bucket, Bucket=bucket
    )
    if response is None:
        return None
    return response.get("Status", "Disabled")


def analyze(bucket, sizes, count, rules, versioning, args):
    """Turn one bucket's raw facts into zero or more findings."""
    total_bytes = sum(sizes.values())
    total_gb = total_bytes / GIB
    monthly_cost = sum(
        (size / GIB) * PRICE_PER_GB.get(storage_type, PRICE_PER_GB["StandardStorage"])
        for storage_type, size in sizes.items()
    )

    record = {
        "bucket": bucket,
        "size_gb": total_gb,
        "objects": count,
        "storage_classes": {k: v / GIB for k, v in sizes.items()},
        "monthly_cost": monthly_cost,
        "versioning": versioning,
        "lifecycle_rules": len(rules) if rules is not None else None,
        "findings": [],
    }

    if total_gb < args.min_gb:
        return record

    enabled = [r for r in (rules or []) if r.get("Status") == "Enabled"]

    if rules is not None and not enabled:
        record["findings"].append(
            {
                "check": "no-lifecycle-policy",
                "detail": "%.1f GB with no enabled lifecycle rule" % total_gb,
                # A transition to Standard-IA saves roughly 45% on cold data. Assume
                # only a third of the bucket is cold enough to qualify.
                "monthly_savings": monthly_cost * 0.15,
                "remediation": "Review access patterns, then add a lifecycle rule "
                "transitioning cold objects to Standard-IA or Glacier.",
            }
        )

    if versioning == "Enabled" and rules is not None:
        has_noncurrent = any(
            "NoncurrentVersionExpiration" in r or "NoncurrentVersionTransitions" in r
            for r in enabled
        )
        if not has_noncurrent:
            record["findings"].append(
                {
                    "check": "versioning-without-expiry",
                    "detail": "versioning is on with no noncurrent-version rule; "
                    "every overwrite is retained forever",
                    # Noncurrent versions are invisible in the size metric's Standard
                    # bucket but bill the same. Unknowable without an inventory report.
                    "monthly_savings": 0.0,
                    "remediation": "Add NoncurrentVersionExpiration to the lifecycle "
                    "policy. Run an S3 Inventory report first to size the exposure.",
                }
            )

    if rules is not None:
        has_abort = any("AbortIncompleteMultipartUpload" in r for r in enabled)
        if not has_abort:
            record["findings"].append(
                {
                    "check": "no-multipart-abort-rule",
                    "detail": "no rule aborting incomplete multipart uploads",
                    "monthly_savings": 0.0,
                    "remediation": "Add an AbortIncompleteMultipartUpload rule with a "
                    "7-day window. Abandoned upload parts are billed but do not appear "
                    "in the console object listing.",
                }
            )

    standard = sizes.get("StandardStorage", 0) / GIB
    if standard >= args.standard_only_gb and standard >= total_gb * 0.95:
        record["findings"].append(
            {
                "check": "large-bucket-standard-only",
                "detail": "%.1f GB entirely in Standard storage" % standard,
                # Intelligent-Tiering moves untouched objects to IA at ~45% less.
                "monthly_savings": standard * PRICE_PER_GB["StandardStorage"] * 0.20,
                "remediation": "Consider Intelligent-Tiering if access patterns are "
                "unpredictable, or a direct transition rule if they are not.",
            }
        )

    return record


def render(payload):
    lines = []
    lines.append("S3 storage footprint — account %s" % payload["identity"]["account"])
    lines.append(
        "%d buckets, %.1f GB total, ~%s/month"
        % (
            payload["bucket_count"],
            payload["total_gb"],
            c.money(payload["total_monthly_cost"]),
        )
    )
    lines.append("")

    buckets = payload["buckets"]
    if not buckets:
        lines.append("No buckets above the size threshold.")
        return "\n".join(lines)

    lines.append("Largest buckets")
    rows = []
    for record in buckets[: payload["params"]["top"]]:
        classes = ", ".join(
            "%s %s" % (k.replace("Storage", ""), c.size_gb(v))
            for k, v in sorted(
                record["storage_classes"].items(), key=lambda kv: kv[1], reverse=True
            )[:3]
        )
        rows.append(
            (
                record["bucket"],
                c.size_gb(record["size_gb"]),
                "{:,}".format(record["objects"]) if record["objects"] is not None else "?",
                c.money(record["monthly_cost"]),
                record["versioning"] or "?",
                classes,
            )
        )
    lines.append(
        c.table(rows, ["Bucket", "Size", "Objects", "Est/mo", "Versioning", "Classes"])
    )
    lines.append("")

    flagged = [r for r in buckets if r["findings"]]
    if flagged:
        lines.append("Findings")
        for record in flagged:
            lines.append("  %s (%.1f GB)" % (record["bucket"], record["size_gb"]))
            for finding in record["findings"]:
                saving = (
                    " ~%s/mo" % c.money(finding["monthly_savings"])
                    if finding["monthly_savings"]
                    else " savings unknown"
                )
                lines.append("    - %s:%s" % (finding["check"], saving))
                lines.append("      %s" % finding["detail"])
                lines.append("      fix: %s" % finding["remediation"])
            lines.append("")

    lines.append(
        "Sizes come from CloudWatch daily metrics and lag by up to 48 hours. Prices are"
    )
    lines.append("us-east-1 list estimates for ranking, not a bill forecast.")
    return "\n".join(lines)


def main():
    args = build_args()
    session = c.build_session(args.profile, args.region)
    identity = c.whoami(session)

    s3 = session.client("s3")
    listing = c.call(s3, "list_buckets", "s3:ListAllMyBuckets")
    buckets = listing.get("Buckets", [])
    if not buckets:
        c.die("this account has no S3 buckets")

    regions = bucket_regions(s3, buckets)
    cloudwatch_clients = {}
    records = []

    for bucket in buckets:
        name = bucket["Name"]
        region = regions.get(name)
        if not region:
            continue
        if region not in cloudwatch_clients:
            cloudwatch_clients[region] = session.client("cloudwatch", region_name=region)
        cloudwatch = cloudwatch_clients[region]

        sizes = storage_by_class(cloudwatch, name)
        if not sizes:
            continue  # Empty, brand new, or metrics not yet published.
        count = object_count(cloudwatch, name)
        rules = lifecycle_rules(s3, name)
        versioning = versioning_state(s3, name)

        record = analyze(name, sizes, count, rules, versioning, args)
        record["region"] = region
        record["created"] = c.iso(bucket.get("CreationDate"))
        if record["size_gb"] >= args.min_gb:
            records.append(record)

    records.sort(key=lambda r: r["size_gb"], reverse=True)

    payload = {
        "identity": identity,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "params": vars(args),
        "bucket_count": len(buckets),
        "buckets": records,
        "total_gb": sum(r["size_gb"] for r in records),
        "total_monthly_cost": sum(r["monthly_cost"] for r in records),
        "total_monthly_savings": sum(
            f["monthly_savings"] for r in records for f in r["findings"]
        ),
    }
    c.emit(payload, args.json, render)


if __name__ == "__main__":
    sys.exit(main())
