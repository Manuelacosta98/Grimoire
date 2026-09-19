#!/usr/bin/env python3
"""Cost Explorer trends: spend by dimension, month-over-month deltas, top movers.

READ-ONLY. Uses only ce:GetCostAndUsage and the ce:Get*Recommendation family.

Cost Explorer bills roughly $0.01 per API request. Responses are cached on local disk
(see --cache-ttl) so repeated runs during a single audit do not pay repeatedly.

Examples
    cost_explorer.py --profile personal --months 3
    cost_explorer.py --profile personal --months 6 --group-by LINKED_ACCOUNT
    cost_explorer.py --profile personal --group-by TAG --tag-key Environment
    cost_explorer.py --profile personal --recommendations
"""

import sys

import _common as c

GROUP_DIMENSIONS = {
    "SERVICE": "Service",
    "LINKED_ACCOUNT": "Account",
    "REGION": "Region",
    "USAGE_TYPE": "Usage type",
    "INSTANCE_TYPE": "Instance type",
    "RECORD_TYPE": "Charge type",
}


def build_args():
    parser = c.base_parser(__doc__.split("\n")[0])
    parser.add_argument(
        "--months", type=int, default=3, help="Whole months of history (default: 3)."
    )
    parser.add_argument(
        "--group-by",
        default="SERVICE",
        choices=sorted(list(GROUP_DIMENSIONS) + ["TAG"]),
        help="Dimension to break spend down by (default: SERVICE).",
    )
    parser.add_argument(
        "--tag-key", help="Tag key to group by. Required when --group-by TAG."
    )
    parser.add_argument(
        "--metric",
        default="UnblendedCost",
        choices=["UnblendedCost", "AmortizedCost", "NetUnblendedCost", "BlendedCost"],
        help="Cost metric (default: UnblendedCost).",
    )
    parser.add_argument(
        "--top", type=int, default=15, help="Rows to show per section (default: 15)."
    )
    parser.add_argument(
        "--exclude-credits",
        action="store_true",
        help="Exclude credits and refunds, showing gross usage spend only.",
    )
    parser.add_argument(
        "--recommendations",
        action="store_true",
        help="Also fetch rightsizing, Savings Plans, and Reserved Instance advice.",
    )
    args = parser.parse_args()
    if args.group_by == "TAG" and not args.tag_key:
        c.die("--group-by TAG also needs --tag-key NAME")
    if args.months < 1:
        c.die("--months must be at least 1")
    return args


def fetch_costs(client, args, start, end):
    """Monthly cost grouped by the chosen dimension."""
    params = {
        "TimePeriod": {"Start": c.iso(start), "End": c.iso(end)},
        "Granularity": "MONTHLY",
        "Metrics": [args.metric],
    }
    if args.group_by == "TAG":
        params["GroupBy"] = [{"Type": "TAG", "Key": args.tag_key}]
    else:
        params["GroupBy"] = [{"Type": "DIMENSION", "Key": args.group_by}]
    if args.exclude_credits:
        params["Filter"] = {
            "Not": {
                "Dimensions": {
                    "Key": "RECORD_TYPE",
                    "Values": ["Credit", "Refund"],
                }
            }
        }

    results = []
    token = None
    while True:
        if token:
            params["NextPageToken"] = token
        page = c.call(client, "get_cost_and_usage", "ce:GetCostAndUsage", **params)
        results.extend(page.get("ResultsByTime", []))
        token = page.get("NextPageToken")
        if not token:
            break
    return results


def shape(results, metric):
    """Fold the API response into {period: {group: amount}} plus a currency."""
    periods = []
    by_period = {}
    currency = "USD"
    for block in results:
        period = block["TimePeriod"]["Start"]
        periods.append(period)
        bucket = by_period.setdefault(period, {})
        for group in block.get("Groups", []):
            key = group["Keys"][0]
            if key.endswith("$"):  # untagged resources come back as "TagKey$"
                key = key[:-1] + "(untagged)"
            amount = group["Metrics"][metric]
            currency = amount.get("Unit", currency)
            bucket[key] = bucket.get(key, 0.0) + float(amount["Amount"])
        total = block.get("Total", {}).get(metric)
        if total and not block.get("Groups"):
            currency = total.get("Unit", currency)
            bucket["(ungrouped)"] = float(total["Amount"])
    return periods, by_period, currency


def movers(periods, by_period):
    """Absolute and percentage change between the last two complete periods."""
    if len(periods) < 2:
        return []
    previous, current = by_period[periods[-2]], by_period[periods[-1]]
    keys = set(previous) | set(current)
    rows = []
    for key in keys:
        was, now = previous.get(key, 0.0), current.get(key, 0.0)
        rows.append(
            {
                "group": key,
                "previous": was,
                "current": now,
                "delta": now - was,
                "delta_pct": c.delta_pct(now, was),
            }
        )
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return rows


def fetch_recommendations(session, region):
    """Optional: what AWS itself thinks you should change. All Get* operations."""
    client = session.client("ce", region_name="us-east-1")
    out = {}

    rightsizing = c.try_call(
        client,
        "get_rightsizing_recommendation",
        "ce:GetRightsizingRecommendation",
        Service="AmazonEC2",
        Configuration={
            "RecommendationTarget": "SAME_INSTANCE_FAMILY",
            "BenefitsConsidered": True,
        },
    )
    if rightsizing:
        summary = rightsizing.get("Summary", {})
        items = []
        for rec in rightsizing.get("RightsizingRecommendations", [])[:25]:
            current = rec.get("CurrentInstance", {})
            items.append(
                {
                    "resource_id": current.get("ResourceId"),
                    "instance_type": (
                        current.get("ResourceDetails", {})
                        .get("EC2ResourceDetails", {})
                        .get("InstanceType")
                    ),
                    "action": rec.get("RightsizingType"),
                    "monthly_savings": float(
                        current.get("MonthlyCost", 0) or 0
                    ),
                }
            )
        out["rightsizing"] = {
            "estimated_monthly_savings": float(
                summary.get("EstimatedTotalMonthlySavingsAmount", 0) or 0
            ),
            "recommendations": items,
        }

    savings_plans = c.try_call(
        client,
        "get_savings_plans_purchase_recommendation",
        "ce:GetSavingsPlansPurchaseRecommendation",
        SavingsPlansType="COMPUTE_SP",
        TermInYears="ONE_YEAR",
        PaymentOption="NO_UPFRONT",
        LookbackPeriodInDays="THIRTY_DAYS",
    )
    if savings_plans:
        detail = savings_plans.get("SavingsPlansPurchaseRecommendation", {}).get(
            "SavingsPlansPurchaseRecommendationSummary", {}
        )
        out["savings_plans"] = {
            "estimated_monthly_savings": float(
                detail.get("EstimatedMonthlySavingsAmount", 0) or 0
            ),
            "estimated_savings_percentage": detail.get("EstimatedSavingsPercentage"),
            "hourly_commitment": detail.get("HourlyCommitmentToPurchase"),
        }

    reserved = c.try_call(
        client,
        "get_reservation_purchase_recommendation",
        "ce:GetReservationPurchaseRecommendation",
        Service="Amazon Relational Database Service",
        TermInYears="ONE_YEAR",
        PaymentOption="NO_UPFRONT",
        LookbackPeriodInDays="THIRTY_DAYS",
    )
    if reserved:
        groups = reserved.get("Recommendations", [])
        total = 0.0
        for group in groups:
            summary = group.get("RecommendationSummary", {})
            total += float(summary.get("TotalEstimatedMonthlySavingsAmount", 0) or 0)
        out["reserved_instances"] = {"estimated_monthly_savings": total}

    return out


def render(payload):
    args = payload["params"]
    lines = []
    lines.append("AWS cost trend — account %s" % payload["identity"]["account"])
    lines.append(
        "%s by %s, %s to %s"
        % (
            args["metric"],
            args["group_by"] if args["group_by"] != "TAG" else "tag:" + args["tag_key"],
            payload["window"]["start"],
            payload["window"]["end"],
        )
    )
    lines.append("")

    currency = payload["currency"]
    periods = payload["periods"]
    totals = payload["totals"]

    lines.append("Monthly total")
    rows = []
    previous = None
    for period in periods:
        total = totals[period]
        change = ""
        if previous is not None:
            pct = c.delta_pct(total, previous)
            if pct is not None:
                change = "%+.1f%%" % pct
        rows.append((period, c.money(total, currency), change))
        previous = total
    lines.append(c.table(rows, ["Month", "Total", "Change"]))
    lines.append("")

    latest = periods[-1]
    label = GROUP_DIMENSIONS.get(args["group_by"], "Group")
    lines.append("Top spend in %s" % latest)
    rows = sorted(
        payload["by_period"][latest].items(), key=lambda kv: kv[1], reverse=True
    )[: args["top"]]
    total_latest = totals[latest] or 1.0
    lines.append(
        c.table(
            [
                (k, c.money(v, currency), "%.1f%%" % (v / total_latest * 100))
                for k, v in rows
            ],
            [label, "Cost", "Share"],
        )
    )
    lines.append("")

    if payload["movers"]:
        lines.append("Biggest movers (%s vs %s)" % (periods[-1], periods[-2]))
        rows = []
        for mover in payload["movers"][: args["top"]]:
            if abs(mover["delta"]) < 0.01:
                continue
            pct = (
                "%+.1f%%" % mover["delta_pct"]
                if mover["delta_pct"] is not None
                else "new"
            )
            rows.append(
                (
                    mover["group"],
                    c.money(mover["previous"], currency),
                    c.money(mover["current"], currency),
                    "%s%s" % ("+" if mover["delta"] >= 0 else "", c.money(mover["delta"], currency)),
                    pct,
                )
            )
        lines.append(c.table(rows, [label, "Previous", "Current", "Delta", "Change"]))
        lines.append("")

    recs = payload.get("recommendations")
    if recs:
        lines.append("AWS recommendations")
        rows = []
        if "rightsizing" in recs:
            rows.append(
                (
                    "EC2 rightsizing",
                    c.money(recs["rightsizing"]["estimated_monthly_savings"], currency),
                    "%d instances flagged"
                    % len(recs["rightsizing"]["recommendations"]),
                )
            )
        if "savings_plans" in recs:
            sp = recs["savings_plans"]
            rows.append(
                (
                    "Compute Savings Plan",
                    c.money(sp["estimated_monthly_savings"], currency),
                    (
                        "commit %s/hr" % sp["hourly_commitment"]
                        if sp.get("hourly_commitment")
                        else "nothing to commit at this usage level"
                    ),
                )
            )
        if "reserved_instances" in recs:
            rows.append(
                (
                    "RDS reserved instances",
                    c.money(
                        recs["reserved_instances"]["estimated_monthly_savings"], currency
                    ),
                    "",
                )
            )
        lines.append(c.table(rows, ["Opportunity", "Est. monthly savings", "Detail"]))
        lines.append("")

    if payload.get("cache_hit"):
        lines.append("(served from cache; pass --no-cache to re-query)")
    return "\n".join(lines)


def main():
    args = build_args()
    session = c.build_session(args.profile, args.region)
    identity = c.whoami(session)

    # Cost Explorer is a global endpoint served out of us-east-1.
    client = session.client("ce", region_name="us-east-1")

    starts = c.month_starts(args.months)
    start, end = starts[0], starts[-1]
    if start == end:
        c.die("--months produced an empty window")

    key = c.cache_key(
        "ce",
        [
            identity["account"],
            c.iso(start),
            c.iso(end),
            args.group_by,
            args.tag_key,
            args.metric,
            args.exclude_credits,
        ],
    )
    hit = {"value": True}

    def produce():
        hit["value"] = False
        return fetch_costs(client, args, start, end)

    results = c.cached(key, args.cache_ttl, produce, enabled=not args.no_cache)

    periods, by_period, currency = shape(results, args.metric)
    if not periods:
        c.die(
            "Cost Explorer returned no data for that window.\n"
            "  A newly enabled account can take up to 24 hours to populate."
        )
    totals = {p: sum(by_period[p].values()) for p in periods}

    payload = {
        "identity": identity,
        "params": vars(args),
        "window": {"start": c.iso(start), "end": c.iso(end)},
        "currency": currency,
        "periods": periods,
        "by_period": by_period,
        "totals": totals,
        "movers": movers(periods, by_period),
        "cache_hit": hit["value"],
    }

    if args.recommendations:
        payload["recommendations"] = fetch_recommendations(session, args.region)

    c.emit(payload, args.json, render)


if __name__ == "__main__":
    sys.exit(main())
