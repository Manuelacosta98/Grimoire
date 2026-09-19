"""Shared helpers for the aws-cost-audit scripts.

READ-ONLY BY CONSTRUCTION. Every AWS call made by this plugin is a get_*, describe_*,
or list_* operation. Nothing here creates, changes, tags, or removes a resource, and
CI fails the build if a mutating boto3 method call appears anywhere in this directory.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

try:
    import boto3
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        NoRegionError,
        ProfileNotFound,
    )
except ImportError:  # pragma: no cover - dependency guidance, not logic
    sys.stderr.write(
        "boto3 is not installed. Install it with:\n"
        "    python3 -m pip install boto3\n"
    )
    raise SystemExit(2)


CACHE_DIR = os.path.join(tempfile.gettempdir(), "grimoire-cost-audit")
DEFAULT_CACHE_TTL = 6 * 3600  # Cost Explorer bills per request; re-runs should be free.


# --------------------------------------------------------------------------- output


def die(message, code=2):
    """Exit with a readable message rather than a traceback."""
    sys.stderr.write("error: %s\n" % message)
    raise SystemExit(code)


def warn(message):
    sys.stderr.write("warning: %s\n" % message)


def money(amount, currency="USD"):
    """Format a float as currency. Small non-zero amounts keep their precision."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return "n/a"
    symbol = "$" if currency == "USD" else currency + " "
    if amount and abs(amount) < 0.01:
        return "%s%.4f" % (symbol, amount)
    return "%s%s" % (symbol, format(round(amount, 2), ",.2f"))


def size_gb(gigabytes):
    """Format a GB figure at a readable scale, so small buckets do not all read 0GB."""
    try:
        gigabytes = float(gigabytes)
    except (TypeError, ValueError):
        return "n/a"
    if gigabytes >= 1024:
        return "%.1fTB" % (gigabytes / 1024.0)
    if gigabytes >= 1:
        return "%.0fGB" % gigabytes
    if gigabytes >= 0.001:
        return "%.0fMB" % (gigabytes * 1024)
    return "%.0fKB" % (gigabytes * 1024 * 1024)


def delta_pct(current, previous):
    """Percent change, guarding the zero-baseline case."""
    if not previous:
        return None
    return (current - previous) / previous * 100.0


def table(rows, headers):
    """Render a list of row-tuples as a plain aligned text table."""
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        cells = [str(c) for c in row]
        str_rows.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for cells in str_rows:
        out.append("  ".join(cells[i].ljust(widths[i]) for i in range(len(cells))).rstrip())
    return "\n".join(out)


def emit(payload, as_json, text_renderer):
    """Print either machine-readable JSON or a human-readable report."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text_renderer(payload))


# ---------------------------------------------------------------------------- args


def base_parser(description):
    """Argument parser with the flags every script in this plugin shares."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--profile",
        help="AWS named profile to use. Omit to use the default credential chain.",
    )
    parser.add_argument(
        "--region",
        help="AWS region. Defaults to the profile's configured region.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a text report."
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the local response cache and re-query AWS.",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help="Cache lifetime in seconds (default: %d)." % DEFAULT_CACHE_TTL,
    )
    return parser


# ------------------------------------------------------------------------- session


def build_session(profile=None, region=None):
    """Return a boto3 Session, failing with a clear message rather than a traceback."""
    kwargs = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    try:
        return boto3.Session(**kwargs)
    except ProfileNotFound:
        available = []
        try:
            available = boto3.Session().available_profiles
        except Exception:  # pragma: no cover - best-effort hint only
            pass
        hint = ("  Available profiles: %s" % ", ".join(available)) if available else ""
        die("AWS profile %r not found.\n%s" % (profile, hint))
    except BotoCoreError as exc:
        die("could not build an AWS session: %s" % exc)


def whoami(session):
    """Identify the account these credentials belong to. Confirm before spending."""
    try:
        ident = session.client("sts").get_caller_identity()
    except NoCredentialsError:
        die(
            "no AWS credentials found.\n"
            "  Pass --profile NAME, or configure the default chain with `aws configure`."
        )
    except ClientError as exc:
        die(explain_client_error(exc, "sts:GetCallerIdentity"))
    return {
        "account": ident.get("Account"),
        "arn": ident.get("Arn"),
        "user_id": ident.get("UserId"),
    }


def explain_client_error(exc, action):
    """Turn a botocore ClientError into a sentence naming the IAM action that failed."""
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    message = exc.response.get("Error", {}).get("Message", str(exc))
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return (
            "access denied calling %s.\n"
            "  These credentials lack that permission. Grant it (read-only) or use a "
            "profile that has it.\n  AWS said: %s" % (action, message)
        )
    if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
        return (
            "credentials have expired calling %s.\n"
            "  Refresh them, for example `aws sso login --profile NAME`." % action
        )
    if code == "OptInRequired":
        return (
            "%s requires opting in for this account.\n"
            "  Cost Explorer must be enabled once in the Billing console before the "
            "API returns data.\n  AWS said: %s" % (action, message)
        )
    if code == "DataUnavailableException":
        return (
            "%s returned no data yet.\n"
            "  Cost Explorer can take up to 24 hours to populate a newly enabled "
            "account." % action
        )
    return "%s failed (%s): %s" % (action, code, message)


def call(client, operation, action_name, **kwargs):
    """Invoke a read-only client operation, translating failures into clear errors."""
    try:
        return getattr(client, operation)(**kwargs)
    except NoCredentialsError:
        die("no AWS credentials found. Pass --profile NAME or run `aws configure`.")
    except NoRegionError:
        die("no AWS region configured. Pass --region, for example --region us-east-1.")
    except ClientError as exc:
        die(explain_client_error(exc, action_name))


class _Expected(object):
    """Sentinel: the call failed with an error code the caller treats as normal."""

    __slots__ = ()

    def __repr__(self):
        return "<expected>"


EXPECTED = _Expected()

DENIAL_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")


def try_call(client, operation, action_name, expected=(), **kwargs):
    """Like call(), but returns None on access denial instead of exiting.

    Used for the optional parts of a sweep, so one missing permission does not
    abandon an otherwise useful audit.

    `expected` lists error codes that are a normal outcome rather than a failure —
    for instance, a bucket with no lifecycle configuration raises
    NoSuchLifecycleConfiguration. Those return the EXPECTED sentinel, so the caller
    can tell "there is genuinely nothing here" apart from "I could not look".
    Conflating the two turns a missing permission into a false finding.
    """
    try:
        return getattr(client, operation)(**kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in expected:
            return EXPECTED
        if code in DENIAL_CODES:
            warn("skipping %s: access denied" % action_name)
            return None
        warn("skipping %s: %s" % (action_name, code or exc))
        return None
    except (BotoCoreError, NoRegionError) as exc:
        warn("skipping %s: %s" % (action_name, exc))
        return None


def paginate(client, operation, result_key, action_name, **kwargs):
    """Collect every page of a paginated read-only operation into one list."""
    items = []
    try:
        if client.can_paginate(operation):
            for page in client.get_paginator(operation).paginate(**kwargs):
                items.extend(page.get(result_key, []) or [])
            return items
        response = getattr(client, operation)(**kwargs)
        return response.get(result_key, []) or []
    except ClientError as exc:
        die(explain_client_error(exc, action_name))
    except NoRegionError:
        die("no AWS region configured. Pass --region, for example --region us-east-1.")


# --------------------------------------------------------------------------- cache


def cache_key(namespace, parts):
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return "%s-%s" % (namespace, digest)


def cached(key, ttl, producer, enabled=True):
    """Memoize a response on local disk.

    Cost Explorer charges roughly $0.01 per API request, so an interactive audit that
    gets re-run while a report is being written should not pay twice for the same data.
    """
    if not enabled or ttl <= 0:
        return producer()
    path = os.path.join(CACHE_DIR, key + ".json")
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl:
            with open(path, "r") as handle:
                return json.load(handle)
    except (OSError, ValueError):
        pass  # A damaged cache entry is not worth failing an audit over.
    value = producer()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(value, handle, default=str)
    except OSError as exc:
        warn("could not write cache entry: %s" % exc)
    return value


# ---------------------------------------------------------------------------- time


def month_starts(months_back):
    """First day of each of the last N whole months, oldest first, plus today."""
    today = date.today()
    cursor = date(today.year, today.month, 1)
    starts = [cursor]
    for _ in range(months_back):
        cursor = date(
            cursor.year - 1 if cursor.month == 1 else cursor.year,
            12 if cursor.month == 1 else cursor.month - 1,
            1,
        )
        starts.append(cursor)
    return sorted(starts)


def iso(value):
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)


def days_ago(value):
    """Whole days between a timezone-aware AWS timestamp and now."""
    if value is None:
        return None
    if isinstance(value, datetime):
        now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
        return (now - value).days
    return None


def utc_window(days):
    """A (start, end) datetime window covering the last N days, UTC-naive."""
    end = datetime.utcnow()
    return end - timedelta(days=days), end
