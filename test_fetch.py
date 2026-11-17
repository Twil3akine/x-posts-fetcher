#!/usr/bin/env python3
"""Offline self-test for fetch.py using canned API responses (no network).

Run: python3 test_fetch.py
"""
import unittest
from datetime import datetime, timezone

import fetch


# ---------------------------------------------------------------------------
# Canned API fixtures
# ---------------------------------------------------------------------------

USER_RESPONSE = {"data": {"id": "111", "username": "account1", "name": "Account One"}}

TWEETS_PAGE_1 = {
    "data": [
        {
            "id": "2001",
            "created_at": "2026-10-25T01:23:45.000Z",
            "text": "hello https://t.co/abc",
            "entities": {
                "urls": [
                    {
                        "start": 6,
                        "end": 22,
                        "url": "https://t.co/abc",
                        "expanded_url": "https://example.com/article",
                        "display_url": "example.com/article",
                    },
                    {
                        "start": 6,
                        "end": 22,
                        "url": "https://t.co/abc",
                        "expanded_url": "https://example.com/article",
                        "display_url": "example.com/article",
                    },
                ]
            },
        },
        {
            "id": "2002",
            "created_at": "2026-10-25T02:00:00.000Z",
            "text": "no links here",
        },
    ],
    "meta": {"result_count": 2, "next_token": "PAGE2"},
}

TWEETS_PAGE_2 = {
    "data": [
        {
            "id": "2003",
            "created_at": "2026-10-25T15:30:00.000Z",
            "text": "second page",
            "entities": {"urls": []},
        }
    ],
    "meta": {"result_count": 1},
}


def fake_api_get(path, token, params=None):
    assert token == "dummy-token", "token must be passed through, never altered"
    if path == "/users/by/username/account1":
        return USER_RESPONSE
    if path == "/users/111/tweets":
        assert params["max_results"] == "100"
        assert params["start_time"] == "2026-10-24T15:00:00Z"
        assert params["end_time"] == "2026-10-25T15:00:00Z"
        if "pagination_token" not in params:
            return TWEETS_PAGE_1
        assert params["pagination_token"] == "PAGE2"
        return TWEETS_PAGE_2
    raise AssertionError(f"unexpected API path: {path}")


class EnvParsingTest(unittest.TestCase):
    def test_parse_env_text(self):
        env = fetch.parse_env_text(
            "# comment\n"
            "X_BEARER_TOKEN=abc123\n"
            'X_ACCOUNTS="account1, account2"\n'
            "export TZ=Asia/Tokyo\n"
            "\n"
            "EMPTY=\n"
        )
        self.assertEqual(env["X_BEARER_TOKEN"], "abc123")
        self.assertEqual(env["X_ACCOUNTS"], "account1, account2")
        self.assertEqual(env["TZ"], "Asia/Tokyo")
        self.assertEqual(env["EMPTY"], "")


class WindowTest(unittest.TestCase):
    def test_previous_day_window_tokyo(self):
        tz = fetch.get_tz("Asia/Tokyo")
        now = datetime(2026, 10, 26, 9, 30, tzinfo=tz)
        start, end = fetch.previous_day_window(tz, now=now)
        # 2026-10-25 00:00 JST == 2026-10-24T15:00:00Z
        self.assertEqual(fetch.format_rfc3339(start), "2026-10-24T15:00:00Z")
        self.assertEqual(fetch.format_rfc3339(end), "2026-10-25T15:00:00Z")

    def test_parse_iso_naive_uses_tz(self):
        tz = fetch.get_tz("Asia/Tokyo")
        dt = fetch.parse_iso("2026-10-25T00:00:00", tz)
        self.assertEqual(fetch.format_rfc3339(dt), "2026-10-24T15:00:00Z")

    def test_parse_iso_with_z(self):
        tz = fetch.get_tz("Asia/Tokyo")
        dt = fetch.parse_iso("2026-10-25T00:00:00Z", tz)
        self.assertEqual(dt, datetime(2026, 10, 25, tzinfo=timezone.utc))


class FetchFlowTest(unittest.TestCase):
    def setUp(self):
        self._original = fetch.api_get
        fetch.api_get = fake_api_get

    def tearDown(self):
        fetch.api_get = self._original

    def test_resolve_and_paginate(self):
        user_id = fetch.resolve_user_id("account1", "dummy-token")
        self.assertEqual(user_id, "111")
        posts = fetch.fetch_posts(
            user_id, "account1", "dummy-token",
            "2026-10-24T15:00:00Z", "2026-10-25T15:00:00Z",
        )
        self.assertEqual([p["id"] for p in posts], ["2001", "2002", "2003"])
        # Expanded URLs are extracted and deduplicated.
        self.assertEqual(posts[0]["urls"], ["https://example.com/article"])
        self.assertEqual(posts[1]["urls"], [])
        self.assertEqual(
            posts[0]["url"], "https://x.com/account1/status/2001"
        )
        self.assertEqual(posts[0]["created_at"], "2026-10-25T01:23:45.000Z")
        self.assertEqual(posts[0]["text"], "hello https://t.co/abc")

    def test_build_result_shape(self):
        class Args:
            from_ = "2026-10-25T00:00:00"
            to = "2026-10-26T00:00:00"

        config = {
            "X_BEARER_TOKEN": "dummy-token",
            "X_ACCOUNTS": "account1",
            "TZ": "Asia/Tokyo",
        }
        result = fetch.build_result(config, Args())
        self.assertEqual(
            result["window"],
            {"start": "2026-10-24T15:00:00Z", "end": "2026-10-25T15:00:00Z"},
        )
        self.assertIn("fetched_at", result)
        self.assertEqual(len(result["accounts"]["account1"]), 3)


class ConfigErrorTest(unittest.TestCase):
    def test_missing_token(self):
        class Args:
            from_ = None
            to = None

        with self.assertRaises(fetch.FetchError):
            fetch.build_result({"X_ACCOUNTS": "account1"}, Args())

    def test_missing_accounts(self):
        class Args:
            from_ = None
            to = None

        with self.assertRaises(fetch.FetchError):
            fetch.build_result({"X_BEARER_TOKEN": "dummy-token"}, Args())


if __name__ == "__main__":
    unittest.main(verbosity=2)
