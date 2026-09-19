#!/usr/bin/env python3
"""Report which cost-audit permissions these credentials actually hold.

READ-ONLY. Probes each action the audit needs with the cheapest real call that
exercises it, and reports allowed, denied, or inconclusive. Run this before an audit
so missing permissions surface up front rather than as warnings halfway through.

Cost Explorer probes are OFF by default because each one bills roughly USD 0.01.
Pass --include-cost-explorer to check those four actions too (about USD 0.04).

Probing by making real calls is deliberate. iam:SimulatePrincipalPolicy would answer
without side effects, but it needs IAM read permissions that an audit role has no
business holding, and it does not account for SCPs, permission boundaries, or resource
policies. An actual call is the ground truth.

Exit codes:  0 every probed action allowed   1 at least one denied   2 setup error

Examples
    preflight.py --profile personal
    preflight.py --profile personal --region eu-west-1 --include-cost-explorer
    preflight.py --profile personal --json
"""

import sys
from datetime import datetime

import _common as c

ALLOWED = "allowed"
DENIED = "denied"
INCONCLUSIVE = "inconclusive"

STATUS_MARK = {ALLOWED: "ok", DENIED: "DENIED", INCONCLUSIVE: "?"}


def credential_kind(arn):
    """Classify how the caller authenticated, from its ARN.

    Long-lived IAM user keys are the weaker option: they do not expire, are the most
    commonly leaked AWS credential, and if they carry broad rights every audit run is
    executed with more power than it needs. An assumed role gives short-lived
    credentials scoped to exactly this job.
    """
    if ":assumed-role/" in arn or ":sts::" in arn:
        return "role", None
    if ":user/" in arn:
        return "user", (
            "These are long-lived IAM user credentials. Prefer assuming a "
            "least-privilege role: deploy iam/cost-audit-role.yaml, then run with a "
            "profile whose role_arn points at it. Short-lived credentials scoped to "
            "this one read-only job are safer than static keys, especially if the "
            "user carries broader rights than the audit needs."
        )
    if arn.endswith(":root"):
        return "root", (
            "These are account root credentials. Never use root for routine work. "
            "Deploy iam/cost-audit-role.yaml and assume that role instead."
        )
    return "unknown", None


def build_args():
    parser = c.base_parser(__doc__.split("\n")[0])
    parser.add_argument(
        "--include-cost-explorer",
        action="store_true",
        help="Also probe the four ce:Get* actions. Bills roughly USD 0.04.",
    )
    return parser.parse_args()


class Probe(object):
    """One IAM action, and the cheapest call that proves whether we hold it."""

    def __init__(self, action, run, needs="", billed=False):
        self.action = action
        self.run = run
        self.needs = needs  # what must exist in the account for this to be conclusive
        self.billed = billed


def classify(fn):
    """Run a probe and map the outcome onto allowed / denied / inconclusive."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        fn()
        return ALLOWED, ""
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in c.DENIAL_CODES:
            return DENIED, code
        if code in ("SubscriptionRequiredException", "OptInRequired"):
            return DENIED, "%s (service not enabled for this account)" % code
        # The call reached the service and was authorised; it just had nothing to say.
        return ALLOWED, "%s (authorised)" % code
    except BotoCoreError as exc:
        return INCONCLUSIVE, str(exc).split("\n")[0][:80]


def build_probes(session, region, account, include_ce):
    """Assemble the probe list. Each call is the smallest one that exercises the action."""
    ec2 = session.client("ec2", region_name=region)
    elb = session.client("elbv2", region_name=region)
    rds = session.client("rds", region_name=region)
    cloudwatch = session.client("cloudwatch", region_name=region)
    s3 = session.client("s3")
    sts = session.client("sts")
    ce = session.client("ce", region_name="us-east-1")

    probes = [
        Probe("sts:GetCallerIdentity", lambda: sts.get_caller_identity()),
        Probe("ec2:DescribeRegions", lambda: ec2.describe_regions()),
        # MaxResults has a floor of 5 on these EC2 operations.
        Probe("ec2:DescribeVolumes", lambda: ec2.describe_volumes(MaxResults=5)),
        Probe("ec2:DescribeAddresses", lambda: ec2.describe_addresses()),
        Probe("ec2:DescribeInstances", lambda: ec2.describe_instances(MaxResults=5)),
        Probe(
            "ec2:DescribeSnapshots",
            lambda: ec2.describe_snapshots(OwnerIds=[account], MaxResults=5),
        ),
        Probe("ec2:DescribeNatGateways", lambda: ec2.describe_nat_gateways(MaxResults=5)),
        Probe("elbv2:DescribeLoadBalancers", lambda: elb.describe_load_balancers(PageSize=1)),
        Probe("elbv2:DescribeTargetGroups", lambda: elb.describe_target_groups(PageSize=1)),
        # MaxRecords has a floor of 20 on RDS.
        Probe("rds:DescribeDBInstances", lambda: rds.describe_db_instances(MaxRecords=20)),
        Probe(
            "cloudwatch:GetMetricStatistics",
            lambda: cloudwatch.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[
                    {"Name": "BucketName", "Value": "grimoire-preflight-probe"},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                StartTime=c.utc_window(2)[0],
                EndTime=c.utc_window(2)[1],
                Period=86400,
                Statistics=["Average"],
            ),
        ),
        Probe("s3:ListAllMyBuckets", lambda: s3.list_buckets()),
    ]

    # The per-bucket probes need a bucket to aim at. Without one they are inconclusive
    # rather than allowed, and saying so is more useful than guessing.
    bucket = None
    try:
        buckets = s3.list_buckets().get("Buckets", [])
        bucket = buckets[0]["Name"] if buckets else None
    except Exception:
        bucket = None

    for action, operation in (
        ("s3:GetBucketLocation", "get_bucket_location"),
        ("s3:GetBucketVersioning", "get_bucket_versioning"),
        ("s3:GetBucketLifecycleConfiguration", "get_bucket_lifecycle_configuration"),
    ):
        if bucket:
            probes.append(
                Probe(action, (lambda op=operation: getattr(s3, op)(Bucket=bucket)))
            )
        else:
            probes.append(Probe(action, None, needs="at least one S3 bucket"))

    # Target health needs a target group to aim at.
    target_group = None
    try:
        groups = elb.describe_target_groups(PageSize=1).get("TargetGroups", [])
        target_group = groups[0]["TargetGroupArn"] if groups else None
    except Exception:
        target_group = None

    if target_group:
        probes.append(
            Probe(
                "elbv2:DescribeTargetHealth",
                lambda: elb.describe_target_health(TargetGroupArn=target_group),
            )
        )
    else:
        probes.append(
            Probe("elbv2:DescribeTargetHealth", None, needs="at least one target group")
        )

    ce_probes = [
        Probe(
            "ce:GetCostAndUsage",
            lambda: ce.get_cost_and_usage(
                TimePeriod={
                    "Start": c.iso(c.month_starts(1)[0]),
                    "End": c.iso(c.month_starts(1)[-1]),
                },
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            ),
            billed=True,
        ),
        Probe(
            "ce:GetRightsizingRecommendation",
            lambda: ce.get_rightsizing_recommendation(Service="AmazonEC2"),
            billed=True,
        ),
        Probe(
            "ce:GetSavingsPlansPurchaseRecommendation",
            lambda: ce.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears="ONE_YEAR",
                PaymentOption="NO_UPFRONT",
                LookbackPeriodInDays="THIRTY_DAYS",
            ),
            billed=True,
        ),
        Probe(
            "ce:GetReservationPurchaseRecommendation",
            lambda: ce.get_reservation_purchase_recommendation(
                Service="Amazon Relational Database Service"
            ),
            billed=True,
        ),
    ]

    if include_ce:
        probes.extend(ce_probes)
    else:
        for probe in ce_probes:
            probe.run = None
            probe.needs = "--include-cost-explorer (each probe bills about USD 0.01)"
            probes.append(probe)

    return probes


def render(payload):
    lines = []
    lines.append("Cost-audit permission preflight")
    lines.append(
        "account %s, region %s" % (payload["identity"]["account"], payload["region"])
    )
    lines.append(
        "as %s (%s credentials)"
        % (payload["identity"]["arn"], payload["credential_kind"])
    )
    if payload.get("credential_advice"):
        lines.append("")
        lines.append("  note: %s" % payload["credential_advice"])
    lines.append("")

    rows = [
        (STATUS_MARK[r["status"]], r["action"], r["note"]) for r in payload["results"]
    ]
    lines.append(c.table(rows, ["", "Action", "Note"]))
    lines.append("")

    counts = payload["summary"]
    lines.append(
        "%d allowed, %d denied, %d inconclusive, of %d actions the audit uses"
        % (
            counts[ALLOWED],
            counts[DENIED],
            counts[INCONCLUSIVE],
            payload["total_actions"],
        )
    )

    denied = [r for r in payload["results"] if r["status"] == DENIED]
    if denied:
        lines.append("")
        lines.append("Denied actions disable these checks:")
        for result in denied:
            lines.append("  %s" % result["action"])
        lines.append("")
        lines.append("To grant exactly what is missing and nothing more, deploy the")
        lines.append("template in this plugin's iam/ directory, or attach")
        lines.append("iam/cost-audit-policy.json to the principal above.")

    inconclusive = [r for r in payload["results"] if r["status"] == INCONCLUSIVE]
    if inconclusive:
        lines.append("")
        lines.append("Could not determine (nothing in the account to probe against):")
        for result in inconclusive:
            lines.append("  %s — needs %s" % (result["action"], result["note"]))

    return "\n".join(lines)


def main():
    args = build_args()
    session = c.build_session(args.profile, args.region)
    identity = c.whoami(session)
    region = args.region or session.region_name or "us-east-1"

    if args.include_cost_explorer and not args.json:
        sys.stderr.write(
            "Probing Cost Explorer: 4 requests, roughly USD 0.04 on this account.\n"
        )

    probes = build_probes(session, region, identity["account"], args.include_cost_explorer)

    results = []
    for probe in probes:
        if probe.run is None:
            results.append(
                {
                    "action": probe.action,
                    "status": INCONCLUSIVE,
                    "note": probe.needs or "not probed",
                }
            )
            continue
        status, note = classify(probe.run)
        results.append({"action": probe.action, "status": status, "note": note})

    results.sort(key=lambda r: r["action"])
    summary = {
        ALLOWED: sum(1 for r in results if r["status"] == ALLOWED),
        DENIED: sum(1 for r in results if r["status"] == DENIED),
        INCONCLUSIVE: sum(1 for r in results if r["status"] == INCONCLUSIVE),
    }

    kind, advice = credential_kind(identity["arn"] or "")
    payload = {
        "identity": identity,
        "credential_kind": kind,
        "credential_advice": advice,
        "region": region,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "results": results,
        "summary": summary,
        "total_actions": len(results),
    }
    c.emit(payload, args.json, render)
    return 1 if summary[DENIED] else 0


if __name__ == "__main__":
    sys.exit(main())
