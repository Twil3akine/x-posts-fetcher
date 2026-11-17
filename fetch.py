#!/usr/bin/env python3
"""x-posts-fetcher: fetch posts of specified accounts via the official X API v2.

Standard library only (no third-party dependencies).

Configuration is read from a `.env` file (see .env.example):
  X_BEARER_TOKEN  (required)  App Bearer Token from the X Developer Portal
  X_ACCOUNTS      (required)  comma-separated usernames, without "@"
  TZ              (optional)  IANA timezone name, default "Asia/Tokyo"

The default fetch window is the previous day in TZ (00:00-24:00 local time).
Use --from / --to (ISO 8601) to override it. The result is a single JSON
document, written to --output PATH or to stdout.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

BASE = "https://api.x.com/2"
MAX_PAGES = 100  # safety cap: 100 pages x 100 posts per account/window
DEFAULT_TZ = "Asia/Tokyo"


class FetchError(Exception):
    """An error that should be reported to the user (never contains secrets)."""


# --------------------------------------------------------------------------
# Configuration (.env)
# --------------------------------------------------------------------------

def parse_env_text(text):
    """Parse simple KEY=VALUE lines. Quotes and inline comments are tolerated."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def load_env():
    """Load .env from the current directory or next to this script.

    Values already present in the process environment take precedence.
    """
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    merged = dict(os.environ)
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for key, value in parse_env_text(fh.read()).items():
                    merged.setdefault(key, value)
            break
    return merged


def get_tz(name):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    # Fallback for environments without tzdata: Asia/Tokyo has no DST.
    if name == "Asia/Tokyo":
        return timezone(timedelta(hours=9), name)
    raise FetchError(f"unknown timezone: {name!r} (tzdata may be missing)")


# --------------------------------------------------------------------------
# Time window
# --------------------------------------------------------------------------

def parse_iso(value, tz):
    """Parse an ISO 8601 timestamp; naive values are interpreted in tz."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise FetchError(f"invalid ISO 8601 timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def previous_day_window(tz, now=None):
    """Return (start, end) datetimes for yesterday 00:00-24:00 in tz."""
    now_local = (now or datetime.now(tz)).astimezone(tz)
    today_start = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    start = today_start - timedelta(days=1)
    return start, today_start


def format_rfc3339(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# X API client
# --------------------------------------------------------------------------

def api_get(path, token, params=None):
    """GET an X API v2 path and return the decoded JSON payload.

    Raises FetchError with a status-bearing message on any failure.
    The token is only ever placed in the Authorization header.
    """
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "x-posts-fetcher/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("title") or ""
                errors = body.get("errors")
                if not detail and errors:
                    detail = "; ".join(
                        str(err.get("message", err)) for err in errors
                    )
        except Exception:
            pass
        message = f"X API request failed: HTTP {e.code} {e.reason}"
        if detail:
            message += f" - {detail}"
        if e.code == 401:
            message += " (check that X_BEARER_TOKEN is a valid Bearer Token)"
        elif e.code == 402:
            message += " (pay-per-use credit balance may be depleted)"
        elif e.code == 429:
            message += " (rate limited; try again later)"
        raise FetchError(message)
    except urllib.error.URLError as e:
        raise FetchError(f"network error while contacting the X API: {e.reason}")


def resolve_user_id(username, token):
    payload = api_get(f"/users/by/username/{urllib.parse.quote(username)}", token)
    data = payload.get("data")
    if not data or "id" not in data:
        raise FetchError(f"could not resolve username {username!r} to a user id")
    return data["id"]


def extract_urls(post):
    urls = []
    for entry in post.get("entities", {}).get("urls", []) or []:
        expanded = entry.get("expanded_url") or entry.get("url")
        if expanded and expanded not in urls:
            urls.append(expanded)
    return urls


def normalize_post(post, username):
    post_id = post["id"]
    return {
        "id": post_id,
        "created_at": post.get("created_at"),
        "text": post.get("text", ""),
        "urls": extract_urls(post),
        "url": f"https://x.com/{username}/status/{post_id}",
    }


def fetch_posts(user_id, username, token, start_rfc, end_rfc):
    """Fetch all posts of a user inside the window, following next_token."""
    posts = []
    pagination_token = None
    for _ in range(MAX_PAGES):
        params = {
            "start_time": start_rfc,
            "end_time": end_rfc,
            "max_results": "100",
            "tweet.fields": "created_at,entities",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        payload = api_get(f"/users/{user_id}/tweets", token, params)
        for post in payload.get("data", []) or []:
            posts.append(normalize_post(post, username))
        pagination_token = payload.get("meta", {}).get("next_token")
        if not pagination_token:
            break
    else:
        raise FetchError(
            f"pagination for @{username} exceeded {MAX_PAGES} pages; "
            "narrow the window and try again"
        )
    return posts


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_result(config, args):
    token = (config.get("X_BEARER_TOKEN") or "").strip()
    if not token:
        raise FetchError("X_BEARER_TOKEN is not set (see .env.example)")

    raw_accounts = (config.get("X_ACCOUNTS") or "").strip()
    accounts = [
        name.strip().lstrip("@")
        for name in raw_accounts.split(",")
        if name.strip()
    ]
    if not accounts:
        raise FetchError("X_ACCOUNTS is not set (see .env.example)")

    tz = get_tz((config.get("TZ") or DEFAULT_TZ).strip() or DEFAULT_TZ)

    if args.from_ or args.to:
        if not (args.from_ and args.to):
            raise FetchError("--from and --to must be given together")
        start = parse_iso(args.from_, tz)
        end = parse_iso(args.to, tz)
        if end <= start:
            raise FetchError("--to must be later than --from")
    else:
        start, end = previous_day_window(tz)

    start_rfc = format_rfc3339(start)
    end_rfc = format_rfc3339(end)

    by_account = {}
    for username in accounts:
        user_id = resolve_user_id(username, token)
        by_account[username] = fetch_posts(
            user_id, username, token, start_rfc, end_rfc
        )

    return {
        "fetched_at": format_rfc3339(datetime.now(timezone.utc)),
        "window": {"start": start_rfc, "end": end_rfc},
        "accounts": by_account,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch posts of specified accounts via the official X API v2."
    )
    parser.add_argument("--from", dest="from_", metavar="ISO8601",
                        help="window start (ISO 8601; naive values use TZ)")
    parser.add_argument("--to", dest="to", metavar="ISO8601",
                        help="window end (ISO 8601; naive values use TZ)")
    parser.add_argument("--output", metavar="PATH",
                        help="write the JSON result to PATH instead of stdout")
    args = parser.parse_args(argv)

    try:
        result = build_result(load_env(), args)
    except FetchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        total = sum(len(posts) for posts in result["accounts"].values())
        print(
            f"wrote {total} post(s) for {len(result['accounts'])} account(s) "
            f"to {args.output}",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
