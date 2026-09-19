#!/usr/bin/env python3
"""Exercise the cost-audit finding logic against synthetic AWS responses.

The scripts are read-only, so their finding paths cannot be tested by pointing them at
a real account unless that account happens to be wasteful. These tests feed the analysis
functions fabricated responses in the shape boto3 returns, and assert on the findings.

Run:  python3 tests/test_cost_audit.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugins",
        "aws-cost-audit",
        "scripts",
    ),
)

import _common as c  # noqa: E402
import cost_explorer  # noqa: E402
import preflight  # noqa: E402
import idle_resources as idle  # noqa: E402
import s3_storage as s3mod  # noqa: E402


class quiet:
    """Suppress the warn() output from probes that are meant to fail."""

    def __enter__(self):
        self._stderr = sys.stderr
        sys.stderr = open(os.devnull, "w")

    def __exit__(self, *exc):
        sys.stderr.close()
        sys.stderr = self._stderr


FAILURES = []


def check(label, actual, expected):
    if actual == expected:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s: got %r, want %r" % (label, actual, expected))
        FAILURES.append(label)


def check_true(label, condition, detail=""):
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


class StubClient:
    """Minimal stand-in for a boto3 client. Never paginates; returns canned responses."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def can_paginate(self, operation):
        return False

    def __getattr__(self, operation):
        def call(**kwargs):
            self.calls.append((operation, kwargs))
            if operation not in self.responses:
                raise AssertionError("unexpected call to %s" % operation)
            return self.responses[operation]

        return call


def days_back(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


# ---------------------------------------------------------------- unattached volumes


def test_unattached_volumes():
    print("unattached_volumes")
    ec2 = StubClient(
        {
            "describe_volumes": {
                "Volumes": [
                    {
                        "VolumeId": "vol-aaa",
                        "Size": 500,
                        "VolumeType": "gp3",
                        "CreateTime": days_back(200),
                        "Tags": [{"Key": "Name", "Value": "old-scratch"}],
                    },
                    {
                        "VolumeId": "vol-bbb",
                        "Size": 100,
                        "VolumeType": "gp2",
                        "CreateTime": days_back(5),
                    },
                ]
            }
        }
    )
    findings = idle.unattached_volumes(ec2, "us-east-1")
    check("count", len(findings), 2)
    check("gp3 500GiB priced at $0.08/GB", findings[0]["monthly_savings"], 40.0)
    check("gp2 100GiB priced at $0.10/GB", findings[1]["monthly_savings"], 10.0)
    check("name tag extracted", findings[0]["name"], "old-scratch")
    check_true(
        "remediation names the volume and region",
        "vol-aaa" in findings[0]["remediation"]
        and "us-east-1" in findings[0]["remediation"],
    )
    check_true("carries a caution", bool(findings[0]["caution"]))
    check("age computed", findings[0]["age_days"], 200)


# ------------------------------------------------------------------- elastic IPs


def test_unassociated_addresses():
    print("unassociated_addresses")
    ec2 = StubClient(
        {
            "describe_addresses": {
                "Addresses": [
                    {"AllocationId": "eipalloc-1", "PublicIp": "1.2.3.4"},
                    {
                        "AllocationId": "eipalloc-2",
                        "PublicIp": "5.6.7.8",
                        "AssociationId": "eipassoc-9",
                    },
                ]
            }
        }
    )
    findings = idle.unassociated_addresses(ec2, "us-east-1")
    check("only the unassociated one is flagged", len(findings), 1)
    check("correct resource", findings[0]["resource"], "eipalloc-1")
    check("priced at the EIP rate", findings[0]["monthly_savings"], idle.PRICE["elastic_ip"])


# -------------------------------------------------------------- stopped instances


def test_stopped_instances():
    print("stopped_instances")
    ec2 = StubClient(
        {
            "describe_instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-123",
                                "InstanceType": "m5.large",
                                "StateTransitionReason": "User initiated",
                                "Tags": [{"Key": "Name", "Value": "forgotten-box"}],
                                "BlockDeviceMappings": [
                                    {"Ebs": {"VolumeId": "vol-x"}},
                                    {"Ebs": {"VolumeId": "vol-y"}},
                                ],
                            }
                        ]
                    }
                ]
            },
            "describe_volumes": {
                "Volumes": [
                    {"VolumeId": "vol-x", "Size": 100, "VolumeType": "gp3"},
                    {"VolumeId": "vol-y", "Size": 50, "VolumeType": "gp3"},
                ]
            },
        }
    )
    findings = idle.stopped_instances(ec2, "us-east-1")
    check("one finding", len(findings), 1)
    check("both volumes priced (150GiB * $0.08)", findings[0]["monthly_savings"], 12.0)
    check_true("detail states the GiB still billed", "150 GiB" in findings[0]["detail"])
    check_true(
        "caution warns termination is irreversible",
        "irreversible" in findings[0]["caution"].lower(),
    )


# ------------------------------------------------------------------- old snapshots


def test_old_snapshots():
    print("old_snapshots")
    ec2 = StubClient(
        {
            "describe_snapshots": {
                "Snapshots": [
                    {
                        "SnapshotId": "snap-old",
                        "VolumeSize": 200,
                        "StartTime": days_back(400),
                        "VolumeId": "vol-gone",
                    },
                    {
                        "SnapshotId": "snap-recent",
                        "VolumeSize": 999,
                        "StartTime": days_back(10),
                        "VolumeId": "vol-live",
                    },
                ]
            }
        }
    )
    findings = idle.old_snapshots(ec2, "us-east-1", "123456789012", 90)
    check("only the old snapshot is flagged", len(findings), 1)
    check("resource", findings[0]["resource"], "snap-old")
    check("priced at $0.05/GB", findings[0]["monthly_savings"], 10.0)
    check_true(
        "caution explains incremental pricing overstates the saving",
        "incremental" in findings[0]["caution"].lower(),
    )


# ------------------------------------------------------------------ S3 bucket rules


def bucket_sizes(standard_gb=0.0, glacier_gb=0.0):
    sizes = {}
    if standard_gb:
        sizes["StandardStorage"] = standard_gb * s3mod.GIB
    if glacier_gb:
        sizes["GlacierStorage"] = glacier_gb * s3mod.GIB
    return sizes


class Args:
    min_gb = 1.0
    standard_only_gb = 100.0
    top = 25


def checks_for(record):
    return sorted(f["check"] for f in record["findings"])


def test_s3_findings():
    print("s3 analyze()")

    # No lifecycle configuration at all.
    record = s3mod.analyze("bare", bucket_sizes(standard_gb=10), 100, [], "Disabled", Args)
    check(
        "bare bucket flags policy and multipart",
        checks_for(record),
        ["no-lifecycle-policy", "no-multipart-abort-rule"],
    )

    # Versioning on, rules present, but nothing expiring noncurrent versions.
    rules = [{"Status": "Enabled", "Expiration": {"Days": 30}}]
    record = s3mod.analyze("versioned", bucket_sizes(standard_gb=10), 100, rules, "Enabled", Args)
    check_true(
        "versioning without expiry is flagged",
        "versioning-without-expiry" in checks_for(record),
    )
    check_true(
        "having a rule suppresses the no-policy finding",
        "no-lifecycle-policy" not in checks_for(record),
    )

    # A complete policy raises nothing.
    complete = [
        {
            "Status": "Enabled",
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }
    ]
    record = s3mod.analyze("tidy", bucket_sizes(standard_gb=10), 100, complete, "Enabled", Args)
    check("a complete policy raises nothing", checks_for(record), [])

    # Large and entirely in Standard.
    record = s3mod.analyze("big", bucket_sizes(standard_gb=500), 10, complete, "Disabled", Args)
    check_true(
        "large Standard-only bucket is flagged",
        "large-bucket-standard-only" in checks_for(record),
    )

    # Mostly Glacier, so not a Standard-only candidate.
    record = s3mod.analyze(
        "cold", bucket_sizes(standard_gb=5, glacier_gb=495), 10, complete, "Disabled", Args
    )
    check_true(
        "a mostly-Glacier bucket is not flagged as Standard-only",
        "large-bucket-standard-only" not in checks_for(record),
    )

    # Below the size threshold, nothing is raised at all.
    record = s3mod.analyze("tiny", bucket_sizes(standard_gb=0.1), 3, [], "Disabled", Args)
    check("sub-threshold bucket raises nothing", checks_for(record), [])

    # Lifecycle unreadable (None) must not be treated as "no policy".
    record = s3mod.analyze("denied", bucket_sizes(standard_gb=10), 5, None, "Enabled", Args)
    check("unreadable lifecycle raises nothing", checks_for(record), [])

    # Cost maths.
    record = s3mod.analyze("priced", bucket_sizes(standard_gb=100), 5, complete, "Disabled", Args)
    check("100GB Standard at $0.023/GB", round(record["monthly_cost"], 4), 2.3)


# --------------------------------------------------------------- cost explorer maths


def test_cost_explorer_shape():
    print("cost_explorer shape() and movers()")
    results = [
        {
            "TimePeriod": {"Start": "2026-07-01"},
            "Groups": [
                {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "100", "Unit": "USD"}}},
                {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "50", "Unit": "USD"}}},
            ],
        },
        {
            "TimePeriod": {"Start": "2026-08-01"},
            "Groups": [
                {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "120", "Unit": "USD"}}},
                {"Keys": ["Amazon RDS"], "Metrics": {"UnblendedCost": {"Amount": "300", "Unit": "USD"}}},
            ],
        },
    ]
    periods, by_period, currency = cost_explorer.shape(results, "UnblendedCost")
    check("periods in order", periods, ["2026-07-01", "2026-08-01"])
    check("currency detected", currency, "USD")
    check("group amount parsed", by_period["2026-08-01"]["Amazon RDS"], 300.0)

    movers = cost_explorer.movers(periods, by_period)
    check("largest mover first", movers[0]["group"], "Amazon RDS")
    check("new service has no percentage baseline", movers[0]["delta_pct"], None)
    by_name = {m["group"]: m for m in movers}
    check("disappeared service shows full drop", by_name["Amazon EC2"]["delta"], -50.0)
    check("grown service percentage", round(by_name["Amazon S3"]["delta_pct"], 1), 20.0)

    # A tag grouping returns "TagKey$" for untagged spend; it must be made readable.
    tagged = [
        {
            "TimePeriod": {"Start": "2026-08-01"},
            "Groups": [
                {"Keys": ["Environment$"], "Metrics": {"UnblendedCost": {"Amount": "9", "Unit": "USD"}}}
            ],
        }
    ]
    _, tag_periods, _ = cost_explorer.shape(tagged, "UnblendedCost")
    check_true(
        "untagged key is made readable",
        "Environment(untagged)" in tag_periods["2026-08-01"],
    )

    check("single period yields no movers", cost_explorer.movers(["a"], {"a": {}}), [])


# ------------------------------------------------------------------ common helpers


def test_common():
    print("_common helpers")
    check("thousands separator", c.money(1234.5), "$1,234.50")
    check("sub-cent precision kept", c.money(0.0004), "$0.0004")
    check("none is n/a", c.money(None), "n/a")
    check("zero baseline is undefined", c.delta_pct(10, 0), None)
    check("percent change", c.delta_pct(120, 100), 20.0)
    check("size scales to MB", c.size_gb(0.5), "512MB")
    check("size scales to TB", c.size_gb(2048), "2.0TB")
    check("empty table", c.table([], ["a"]), "(none)")
    check("month_starts spans N+1 boundaries", len(c.month_starts(3)), 4)




# ------------------------------------------------------- lifecycle three-state logic


class ErrorClient:
    """Stub that raises a botocore ClientError with a chosen code."""

    def __init__(self, code):
        self.code = code

    def __getattr__(self, operation):
        def call(**kwargs):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": self.code, "Message": "stub"}}, operation
            )

        return call


def test_lifecycle_three_states():
    print("lifecycle_rules() keeps its three outcomes distinct")

    # A bucket with rules returns them.
    ok = StubClient({"get_bucket_lifecycle_configuration": {"Rules": [{"Status": "Enabled"}]}})
    check("rules are returned", s3mod.lifecycle_rules(ok, "b"), [{"Status": "Enabled"}])

    # No configuration at all is a finding, not an error: [] not None.
    none_configured = ErrorClient("NoSuchLifecycleConfiguration")
    check("no configuration yields []", s3mod.lifecycle_rules(none_configured, "b"), [])

    # Denied is an unknown: None, so the caller must not claim "no policy".
    with quiet():
        result = s3mod.lifecycle_rules(ErrorClient("AccessDenied"), "b")
    check("access denied yields None", result, None)

    # And the distinction must survive into the findings: a denied bucket raises nothing.
    record = s3mod.analyze("denied", bucket_sizes(standard_gb=50), 10, None, "Enabled", Args)
    check_true(
        "unreadable policy produces no false no-policy finding",
        "no-lifecycle-policy" not in checks_for(record),
    )
    record = s3mod.analyze("bare", bucket_sizes(standard_gb=50), 10, [], "Enabled", Args)
    check_true(
        "genuinely absent policy does produce the finding",
        "no-lifecycle-policy" in checks_for(record),
    )


def test_try_call_expected():
    print("try_call() expected-code sentinel")
    client = ErrorClient("NoSuchLifecycleConfiguration")
    result = c.try_call(
        client, "get_bucket_lifecycle_configuration", "s3:X",
        expected=("NoSuchLifecycleConfiguration",),
    )
    check_true("expected code returns the sentinel", result is c.EXPECTED)
    check_true("sentinel is not None", result is not None)

    with quiet():
        result = c.try_call(client, "get_bucket_lifecycle_configuration", "s3:X")
    check_true("without expected=, the same code returns None", result is None)

    with quiet():
        denied = c.try_call(
            ErrorClient("AccessDenied"), "op", "s3:X",
            expected=("NoSuchLifecycleConfiguration",),
        )
    check_true("a denial still returns None even with expected=", denied is None)


# ----------------------------------------------------------------- preflight logic


def test_credential_kind():
    print("preflight credential_kind()")
    kind, advice = preflight.credential_kind(
        "arn:aws:sts::123456789012:assumed-role/GrimoireCostAuditor/session"
    )
    check("assumed role is recognised", kind, "role")
    check_true("a role needs no advice", advice is None)

    kind, advice = preflight.credential_kind("arn:aws:iam::123456789012:user/manuel")
    check("iam user is recognised", kind, "user")
    check_true("a user gets advice", bool(advice) and "least-privilege role" in advice)

    kind, advice = preflight.credential_kind("arn:aws:iam::123456789012:root")
    check("root is recognised", kind, "root")
    check_true("root gets advice", bool(advice) and "Never use root" in advice)


def test_preflight_classify():
    print("preflight classify()")
    from botocore.exceptions import ClientError

    def raiser(code):
        def fn():
            raise ClientError({"Error": {"Code": code, "Message": "m"}}, "Op")

        return fn

    check("success is allowed", preflight.classify(lambda: None)[0], preflight.ALLOWED)
    check("AccessDenied is denied", preflight.classify(raiser("AccessDenied"))[0], preflight.DENIED)
    check(
        "UnauthorizedOperation is denied",
        preflight.classify(raiser("UnauthorizedOperation"))[0],
        preflight.DENIED,
    )
    check(
        "OptInRequired is denied",
        preflight.classify(raiser("OptInRequired"))[0],
        preflight.DENIED,
    )
    # Reaching the service and being told the input was wrong still proves authorisation.
    check(
        "ValidationException means authorised",
        preflight.classify(raiser("ValidationException"))[0],
        preflight.ALLOWED,
    )


# --------------------------------------------------------- policy matches the code


def test_policy_matches_code():
    print("IAM policy covers exactly the actions the code names")
    import json as _json
    import re as _re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scripts = os.path.join(root, "plugins", "aws-cost-audit", "scripts")
    iam = os.path.join(root, "plugins", "aws-cost-audit", "iam")

    named = set()
    for filename in sorted(os.listdir(scripts)):
        if filename.endswith(".py"):
            with open(os.path.join(scripts, filename)) as handle:
                named.update(
                    _re.findall(r'"([a-z0-9]+:[A-Z][A-Za-z]+)', handle.read())
                )

    policy = _json.load(open(os.path.join(iam, "cost-audit-policy.json")))
    granted = set(a for st in policy["Statement"] for a in st["Action"])

    check("policy grants nothing the code does not use", sorted(granted - named), [])
    check("policy covers every action the code names", sorted(named - granted), [])

    with open(os.path.join(iam, "cost-audit-role.yaml")) as handle:
        templated = set(
            _re.findall(r"^\s*-\s+([a-z0-9]+:[A-Z][A-Za-z]+)\s*$", handle.read(), _re.M)
        )
    check("template matches the policy", sorted(templated ^ granted), [])

    check_true("every action is a read", all(
        a.split(":")[1].startswith(("Get", "Describe", "List")) for a in granted
    ), sorted(a for a in granted if not a.split(":")[1].startswith(("Get", "Describe", "List"))))


def main():
    for test in (
        test_common,
        test_unattached_volumes,
        test_unassociated_addresses,
        test_stopped_instances,
        test_old_snapshots,
        test_s3_findings,
        test_cost_explorer_shape,
        test_lifecycle_three_states,
        test_try_call_expected,
        test_credential_kind,
        test_preflight_classify,
        test_policy_matches_code,
    ):
        test()
    print()
    if FAILURES:
        print("%d check(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
