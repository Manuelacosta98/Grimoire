#!/usr/bin/env python3
"""Sweep a region for idle and orphaned resources that still cost money.

READ-ONLY. Uses only describe_*, list_*, and get_metric_statistics operations.

Checks performed:
  - EBS volumes in the `available` state (unattached, still billed)
  - Elastic IPs not associated with anything
  - Stopped EC2 instances still paying for their attached storage
  - EBS snapshots older than --snapshot-age days
  - NAT gateways passing almost no traffic
  - Load balancers with no healthy targets
  - RDS instances with no database connections

Savings figures are ESTIMATES from us-east-1 list prices. They are meant for ranking
findings, not for forecasting a bill. Confirm against Cost Explorer before acting.

Examples
    idle_resources.py --profile personal --region us-east-1
    idle_resources.py --profile personal --all-regions --json
"""

import sys
from datetime import datetime

import _common as c

# Approximate us-east-1 list prices, USD per month. Ranking aid, not a quote.
PRICE = {
    "ebs_gp3_gb": 0.08,
    "ebs_gp2_gb": 0.10,
    "ebs_io1_gb": 0.125,
    "ebs_st1_gb": 0.045,
    "ebs_sc1_gb": 0.015,
    "ebs_standard_gb": 0.05,
    "snapshot_gb": 0.05,
    "elastic_ip": 3.65,
    "nat_gateway": 32.85,
    "load_balancer": 16.43,
}

HOURS_PER_MONTH = 730.0


def build_args():
    parser = c.base_parser(__doc__.split("\n")[0])
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Sweep every enabled region instead of just one. Slower.",
    )
    parser.add_argument(
        "--snapshot-age",
        type=int,
        default=90,
        help="Flag snapshots older than this many days (default: 90).",
    )
    parser.add_argument(
        "--idle-days",
        type=int,
        default=14,
        help="CloudWatch lookback for idleness checks (default: 14).",
    )
    parser.add_argument(
        "--min-savings",
        type=float,
        default=0.0,
        help="Hide findings below this estimated monthly saving.",
    )
    return parser.parse_args()


def volume_monthly_cost(volume):
    size = volume.get("Size", 0) or 0
    key = "ebs_%s_gb" % (volume.get("VolumeType") or "gp2")
    return size * PRICE.get(key, PRICE["ebs_gp2_gb"])


def name_of(resource):
    for tag in resource.get("Tags", []) or []:
        if tag.get("Key") == "Name":
            return tag.get("Value")
    return ""


# ------------------------------------------------------------------------- checks


def unattached_volumes(ec2, region):
    volumes = c.paginate(
        ec2,
        "describe_volumes",
        "Volumes",
        "ec2:DescribeVolumes",
        Filters=[{"Name": "status", "Values": ["available"]}],
    )
    findings = []
    for volume in volumes:
        findings.append(
            {
                "check": "unattached-ebs-volume",
                "region": region,
                "resource": volume["VolumeId"],
                "name": name_of(volume),
                "detail": "%s GiB %s, unattached since %s"
                % (
                    volume.get("Size"),
                    volume.get("VolumeType"),
                    c.iso(volume.get("CreateTime")),
                ),
                "age_days": c.days_ago(volume.get("CreateTime")),
                "monthly_savings": volume_monthly_cost(volume),
                "remediation": "aws ec2 delete-volume --volume-id %s --region %s"
                % (volume["VolumeId"], region),
                "caution": "Snapshot it first if the data might still be needed.",
            }
        )
    return findings


def unassociated_addresses(ec2, region):
    response = c.try_call(ec2, "describe_addresses", "ec2:DescribeAddresses")
    if not response:
        return []
    findings = []
    for address in response.get("Addresses", []):
        if address.get("AssociationId"):
            continue
        findings.append(
            {
                "check": "unassociated-elastic-ip",
                "region": region,
                "resource": address.get("AllocationId") or address.get("PublicIp"),
                "name": name_of(address),
                "detail": "%s is allocated but not associated" % address.get("PublicIp"),
                "age_days": None,
                "monthly_savings": PRICE["elastic_ip"],
                "remediation": "aws ec2 release-address --allocation-id %s --region %s"
                % (address.get("AllocationId"), region),
                "caution": "Releasing loses the address permanently.",
            }
        )
    return findings


def stopped_instances(ec2, region):
    reservations = c.paginate(
        ec2,
        "describe_instances",
        "Reservations",
        "ec2:DescribeInstances",
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}],
    )
    instances = [i for r in reservations for i in r.get("Instances", [])]
    if not instances:
        return []

    volume_ids = [
        mapping["Ebs"]["VolumeId"]
        for instance in instances
        for mapping in instance.get("BlockDeviceMappings", [])
        if mapping.get("Ebs", {}).get("VolumeId")
    ]
    sizes = {}
    if volume_ids:
        for volume in c.paginate(
            ec2,
            "describe_volumes",
            "Volumes",
            "ec2:DescribeVolumes",
            VolumeIds=volume_ids,
        ):
            sizes[volume["VolumeId"]] = volume

    findings = []
    for instance in instances:
        attached = [
            sizes.get(m["Ebs"]["VolumeId"])
            for m in instance.get("BlockDeviceMappings", [])
            if m.get("Ebs", {}).get("VolumeId") in sizes
        ]
        storage_cost = sum(volume_monthly_cost(v) for v in attached if v)
        total_gb = sum((v.get("Size") or 0) for v in attached if v)
        transition = instance.get("StateTransitionReason", "")
        findings.append(
            {
                "check": "stopped-instance-paying-storage",
                "region": region,
                "resource": instance["InstanceId"],
                "name": name_of(instance),
                "detail": "%s stopped, %d GiB of EBS still billed (%s)"
                % (instance.get("InstanceType"), total_gb, transition or "no timestamp"),
                "age_days": None,
                "monthly_savings": storage_cost,
                "remediation": "aws ec2 terminate-instances --instance-ids %s --region %s"
                % (instance["InstanceId"], region),
                "caution": "Terminating is irreversible. Confirm the instance is truly "
                "abandoned, and snapshot its volumes first.",
            }
        )
    return findings


def old_snapshots(ec2, region, account, max_age):
    snapshots = c.paginate(
        ec2,
        "describe_snapshots",
        "Snapshots",
        "ec2:DescribeSnapshots",
        OwnerIds=[account],
    )
    findings = []
    for snapshot in snapshots:
        age = c.days_ago(snapshot.get("StartTime"))
        if age is None or age < max_age:
            continue
        size = snapshot.get("VolumeSize", 0) or 0
        findings.append(
            {
                "check": "old-snapshot",
                "region": region,
                "resource": snapshot["SnapshotId"],
                "name": name_of(snapshot),
                "detail": "%d GiB snapshot, %d days old, of %s"
                % (size, age, snapshot.get("VolumeId") or "a deleted volume"),
                "age_days": age,
                # Snapshots are incremental; full-size pricing overstates the saving.
                "monthly_savings": size * PRICE["snapshot_gb"],
                "remediation": "aws ec2 delete-snapshot --snapshot-id %s --region %s"
                % (snapshot["SnapshotId"], region),
                "caution": "Snapshots are incremental, so the real saving is usually "
                "lower than shown. Check it is not the base of a later snapshot or an AMI.",
            }
        )
    findings.sort(key=lambda f: f["monthly_savings"], reverse=True)
    return findings


def metric_sum(cloudwatch, namespace, metric, dimensions, days, statistic="Sum"):
    start, end = c.utc_window(days)
    response = c.try_call(
        cloudwatch,
        "get_metric_statistics",
        "cloudwatch:GetMetricStatistics",
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=[statistic],
    )
    if not response:
        return None
    points = response.get("Datapoints", [])
    if not points:
        return None
    return sum(p[statistic] for p in points)


def idle_nat_gateways(ec2, cloudwatch, region, days):
    gateways = c.paginate(
        ec2,
        "describe_nat_gateways",
        "NatGateways",
        "ec2:DescribeNatGateways",
        Filter=[{"Name": "state", "Values": ["available"]}],
    )
    findings = []
    for gateway in gateways:
        gateway_id = gateway["NatGatewayId"]
        total_bytes = metric_sum(
            cloudwatch,
            "AWS/NATGateway",
            "BytesOutToDestination",
            [{"Name": "NatGatewayId", "Value": gateway_id}],
            days,
        )
        if total_bytes is None:
            continue
        megabytes = total_bytes / (1024.0 * 1024.0)
        if megabytes > 100:
            continue
        findings.append(
            {
                "check": "idle-nat-gateway",
                "region": region,
                "resource": gateway_id,
                "name": name_of(gateway),
                "detail": "%.1f MB egress over %d days" % (megabytes, days),
                "age_days": c.days_ago(gateway.get("CreateTime")),
                "monthly_savings": PRICE["nat_gateway"],
                "remediation": "aws ec2 delete-nat-gateway --nat-gateway-id %s --region %s"
                % (gateway_id, region),
                "caution": "Private subnets routed through this gateway lose outbound "
                "internet access. Check the route tables first.",
            }
        )
    return findings


def empty_load_balancers(session, region, account):
    elb = session.client("elbv2", region_name=region)
    balancers = c.paginate(
        elb, "describe_load_balancers", "LoadBalancers", "elbv2:DescribeLoadBalancers"
    )
    if not balancers:
        return []
    groups = c.paginate(
        elb, "describe_target_groups", "TargetGroups", "elbv2:DescribeTargetGroups"
    )
    by_balancer = {}
    for group in groups:
        for arn in group.get("LoadBalancerArns", []) or []:
            by_balancer.setdefault(arn, []).append(group)

    findings = []
    for balancer in balancers:
        arn = balancer["LoadBalancerArn"]
        healthy = 0
        for group in by_balancer.get(arn, []):
            health = c.try_call(
                elb,
                "describe_target_health",
                "elbv2:DescribeTargetHealth",
                TargetGroupArn=group["TargetGroupArn"],
            )
            if not health:
                continue
            for target in health.get("TargetHealthDescriptions", []):
                if target.get("TargetHealth", {}).get("State") == "healthy":
                    healthy += 1
        if healthy:
            continue
        findings.append(
            {
                "check": "load-balancer-without-healthy-targets",
                "region": region,
                "resource": balancer["LoadBalancerName"],
                "name": balancer["LoadBalancerName"],
                "detail": "%s load balancer, %d target groups, 0 healthy targets"
                % (balancer.get("Type", "?"), len(by_balancer.get(arn, []))),
                "age_days": c.days_ago(balancer.get("CreatedTime")),
                "monthly_savings": PRICE["load_balancer"],
                "remediation": "aws elbv2 delete-load-balancer --load-balancer-arn %s "
                "--region %s" % (arn, region),
                "caution": "A balancer fronting an autoscaling group that is scaled to "
                "zero will also show no healthy targets. Confirm it is genuinely unused.",
            }
        )
    return findings


def idle_databases(session, region, days):
    rds = session.client("rds", region_name=region)
    instances = c.paginate(
        rds, "describe_db_instances", "DBInstances", "rds:DescribeDBInstances"
    )
    if not instances:
        return []
    cloudwatch = session.client("cloudwatch", region_name=region)
    findings = []
    for instance in instances:
        if instance.get("DBInstanceStatus") != "available":
            continue
        identifier = instance["DBInstanceIdentifier"]
        connections = metric_sum(
            cloudwatch,
            "AWS/RDS",
            "DatabaseConnections",
            [{"Name": "DBInstanceIdentifier", "Value": identifier}],
            days,
        )
        if connections is None or connections > 0:
            continue
        findings.append(
            {
                "check": "idle-rds-instance",
                "region": region,
                "resource": identifier,
                "name": identifier,
                "detail": "%s, %d GiB, zero connections over %d days"
                % (
                    instance.get("DBInstanceClass"),
                    instance.get("AllocatedStorage", 0),
                    days,
                ),
                "age_days": c.days_ago(instance.get("InstanceCreateTime")),
                # Instance-class pricing varies too much to guess; storage only.
                "monthly_savings": (instance.get("AllocatedStorage", 0) or 0) * 0.115,
                "remediation": "Review in the RDS console before acting: "
                "aws rds describe-db-instances --db-instance-identifier %s --region %s"
                % (identifier, region),
                "caution": "Storage cost only; the compute cost is larger and depends on "
                "the instance class. A warm standby or DR replica is idle on purpose.",
            }
        )
    return findings


# -------------------------------------------------------------------------- driver


def sweep_region(session, region, account, args):
    ec2 = session.client("ec2", region_name=region)
    cloudwatch = session.client("cloudwatch", region_name=region)
    findings = []
    findings += unattached_volumes(ec2, region)
    findings += unassociated_addresses(ec2, region)
    findings += stopped_instances(ec2, region)
    findings += old_snapshots(ec2, region, account, args.snapshot_age)
    findings += idle_nat_gateways(ec2, cloudwatch, region, args.idle_days)
    findings += empty_load_balancers(session, region, account)
    findings += idle_databases(session, region, args.idle_days)
    return findings


def enabled_regions(session):
    ec2 = session.client("ec2", region_name="us-east-1")
    response = c.call(ec2, "describe_regions", "ec2:DescribeRegions")
    return sorted(r["RegionName"] for r in response.get("Regions", []))


def render(payload):
    lines = []
    lines.append("Idle and orphaned resources — account %s" % payload["identity"]["account"])
    lines.append("Regions swept: %s" % ", ".join(payload["regions"]))
    lines.append(
        "Estimated monthly waste: %s across %d findings"
        % (c.money(payload["total_monthly_savings"]), len(payload["findings"]))
    )
    lines.append("")

    if not payload["findings"]:
        lines.append("Nothing flagged. Either the account is clean or the checks lacked")
        lines.append("permissions — re-run without --json to see any skip warnings.")
        return "\n".join(lines)

    by_check = {}
    for finding in payload["findings"]:
        by_check.setdefault(finding["check"], []).append(finding)

    ordered = sorted(
        by_check.items(),
        key=lambda kv: sum(f["monthly_savings"] for f in kv[1]),
        reverse=True,
    )
    for check, group in ordered:
        subtotal = sum(f["monthly_savings"] for f in group)
        lines.append(
            "%s — %d found, ~%s/month" % (check, len(group), c.money(subtotal))
        )
        rows = [
            (
                f["region"],
                f["resource"],
                (f["name"] or "")[:24],
                c.money(f["monthly_savings"]),
                f["detail"],
            )
            for f in sorted(group, key=lambda f: f["monthly_savings"], reverse=True)[:20]
        ]
        lines.append(c.table(rows, ["Region", "Resource", "Name", "Est/mo", "Detail"]))
        lines.append("  caution: %s" % group[0]["caution"])
        lines.append("")

    lines.append(
        "Savings are estimates from us-east-1 list prices. Confirm in Cost Explorer."
    )
    return "\n".join(lines)


def main():
    args = build_args()
    session = c.build_session(args.profile, args.region)
    identity = c.whoami(session)

    if args.all_regions:
        regions = enabled_regions(session)
    else:
        region = args.region or session.region_name
        if not region:
            c.die("no region configured. Pass --region, for example --region us-east-1.")
        regions = [region]

    findings = []
    for region in regions:
        findings.extend(sweep_region(session, region, identity["account"], args))

    findings = [f for f in findings if f["monthly_savings"] >= args.min_savings]
    findings.sort(key=lambda f: f["monthly_savings"], reverse=True)

    payload = {
        "identity": identity,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "regions": regions,
        "params": vars(args),
        "findings": findings,
        "total_monthly_savings": sum(f["monthly_savings"] for f in findings),
    }
    c.emit(payload, args.json, render)


if __name__ == "__main__":
    sys.exit(main())
