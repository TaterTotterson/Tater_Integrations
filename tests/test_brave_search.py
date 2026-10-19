from __future__ import annotations

import sys
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

helpers = types.ModuleType("helpers")
helpers.redis_client = None
sys.modules.setdefault("helpers", helpers)

from integrations import brave_search


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, *, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def get(self, key: str) -> str | None:
        return self.strings.get(key)


class BraveSearchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        brave_search.redis_client = self.redis

    def configure(self) -> None:
        self.redis.hashes[brave_search.BRAVE_SEARCH_SETTINGS_KEY] = {
            "BRAVE_API_KEY": "test-api-key",
        }

    def test_settings_round_trip_and_status(self) -> None:
        self.assertFalse(brave_search.integration_status()["configured"])

        saved = brave_search.save_brave_search_settings(api_key="new-key")

        self.assertEqual(saved["BRAVE_API_KEY"], "new-key")
        self.assertTrue(brave_search.integration_status()["configured"])

    def test_search_enables_spellcheck_and_normalizes_corrections_and_locations(self) -> None:
        self.configure()
        observed: dict[str, object] = {}
        payload = {
            "query": {
                "original": "what time does khols close in eastvale",
                "altered": "what time does kohls close in eastvale",
                "cleaned": "what time does kohls close in eastvale",
                "spellcheck_off": False,
                "more_results_available": True,
            },
            "locations": {
                "results": [
                    {
                        "title": "Kohl's Eastvale",
                        "url": "https://www.kohls.com/stores/ca/eastvale-123.shtml",
                        "description": "Department store",
                        "postal_address": {
                            "displayAddress": "123 Main St, Eastvale, CA 91752",
                        },
                        "opening_hours": {
                            "current_day": [
                                {
                                    "full_name": "Sunday",
                                    "opens": "09:00",
                                    "closes": "21:00",
                                }
                            ],
                        },
                        "contact": {"telephone": "+1 555-0100"},
                        "rating": {
                            "ratingValue": 4.2,
                            "bestRating": 5,
                            "reviewCount": 120,
                        },
                    }
                ]
            },
            "web": {
                "results": [
                    {
                        "title": "Kohl's Store Locator",
                        "url": "https://www.kohls.com/stores.shtml",
                        "description": "Find a Kohl's store.",
                    },
                    {
                        "title": "Extra result",
                        "url": "https://example.com/extra",
                        "description": "Another result.",
                    },
                ]
            },
        }

        def fake_read_json(request, *, timeout_val: int):
            observed["params"] = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            observed["headers"] = dict(request.header_items())
            observed["timeout"] = timeout_val
            return payload, None

        with patch.object(brave_search, "_read_json", side_effect=fake_read_json):
            result = brave_search.integration_web_search(
                "what time does khols close in eastvale",
                num_results=2,
                start=3,
                safe="off",
                country="US",
                language="en",
                timeout_sec=9,
            )

        params = observed["params"]
        self.assertEqual(params["q"], ["what time does khols close in eastvale"])
        self.assertEqual(params["count"], ["2"])
        self.assertEqual(params["offset"], ["1"])
        self.assertEqual(params["safesearch"], ["off"])
        self.assertEqual(params["spellcheck"], ["true"])
        self.assertEqual(params["country"], ["us"])
        self.assertEqual(params["search_lang"], ["en"])
        self.assertEqual(observed["timeout"], 9)

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "brave_search")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["next_start"], 5)
        self.assertEqual(result["original_query"], "what time does khols close in eastvale")
        self.assertEqual(result["corrected_query"], "what time does kohls close in eastvale")
        self.assertEqual(result["effective_query"], "what time does kohls close in eastvale")
        self.assertTrue(result["spellcheck_applied"])
        self.assertFalse(result["spellcheck_off"])
        self.assertEqual(result["results"][0]["title"], "Kohl's Eastvale")
        self.assertIn("Hours today: Sunday 09:00-21:00", result["results"][0]["snippet"])
        self.assertIn("Address: 123 Main St", result["results"][0]["snippet"])
        self.assertIn("Rating: 4.2/5 (120 reviews)", result["results"][0]["snippet"])
        self.assertEqual(result["results"][1]["title"], "Kohl's Store Locator")

    def test_search_deduplicates_location_and_web_results(self) -> None:
        self.configure()
        url = "https://example.com/store"
        payload = {
            "query": {"original": "example store", "more_results_available": False},
            "locations": {"results": [{"title": "Local store", "url": url}]},
            "web": {
                "results": [
                    {"title": "Duplicate", "url": url},
                    {"title": "Second result", "url": "https://example.org/second"},
                ]
            },
        }

        with patch.object(brave_search, "_read_json", return_value=(payload, None)):
            result = brave_search.integration_web_search("example store", num_results=5)

        self.assertEqual([item["title"] for item in result["results"]], ["Local store", "Second result"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_start"])
        self.assertFalse(result["spellcheck_applied"])

    def test_local_hours_query_recovers_with_autosuggest(self) -> None:
        self.configure()
        calls: list[tuple[str, dict[str, list[str]]]] = []
        initial_payload = {
            "query": {
                "original": "what time does Cole's closed in east bell",
                "spellcheck_off": False,
                "more_results_available": False,
            },
            "web": {
                "results": [
                    {
                        "title": "Cole Elementary - Bell Schedule",
                        "url": "https://school.example/bell-schedule",
                        "description": "School schedule and announcements.",
                    }
                ]
            },
        }
        suggest_payload = {
            "type": "suggest",
            "query": {"original": "Cole's east bell"},
            "results": [
                {"query": "kohl's eastvale"},
                {"query": "cole elementary bell schedule"},
            ],
        }
        recovery_payload = {
            "query": {
                "original": "kohl's eastvale hours",
                "spellcheck_off": False,
                "more_results_available": False,
            },
            "locations": {
                "results": [
                    {
                        "title": "Kohl's Eastvale",
                        "url": "https://www.kohls.com/stores/ca/eastvale.shtml",
                        "postal_address": {"displayAddress": "Eastvale, CA"},
                        "opening_hours": {
                            "current_day": [
                                {"full_name": "Sunday", "opens": "09:00", "closes": "21:00"}
                            ]
                        },
                    }
                ]
            },
            "web": {"results": []},
        }

        def fake_read_json(request, *, timeout_val: int):
            parsed = urllib.parse.urlparse(request.full_url)
            params = urllib.parse.parse_qs(parsed.query)
            calls.append((parsed.path, params))
            if parsed.path.endswith("/suggest/search"):
                return suggest_payload, None
            if params["q"] == ["kohl's eastvale hours"]:
                return recovery_payload, None
            return initial_payload, None

        with patch.object(brave_search, "_read_json", side_effect=fake_read_json):
            result = brave_search.integration_web_search(
                "what time does Cole's closed in east bell",
                country="us",
                language="en",
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], "/res/v1/web/search")
        self.assertEqual(calls[1][0], "/res/v1/suggest/search")
        self.assertEqual(calls[1][1]["q"], ["Cole's east bell"])
        self.assertEqual(calls[1][1]["count"], ["5"])
        self.assertEqual(calls[1][1]["country"], ["US"])
        self.assertEqual(calls[1][1]["lang"], ["en"])
        self.assertEqual(calls[2][1]["q"], ["kohl's eastvale hours"])
        self.assertTrue(result["autosuggest_attempted"])
        self.assertEqual(result["autosuggest_query"], "Cole's east bell")
        self.assertEqual(result["suggested_query"], "kohl's eastvale")
        self.assertTrue(result["recovery_applied"])
        self.assertEqual(result["recovery_query"], "kohl's eastvale hours")
        self.assertEqual(result["effective_query"], "kohl's eastvale hours")
        self.assertEqual(result["results"][0]["title"], "Kohl's Eastvale")
        self.assertIn("Hours today: Sunday 09:00-21:00", result["results"][0]["snippet"])

    def test_autosuggest_keeps_original_results_without_a_close_suggestion(self) -> None:
        self.configure()
        initial_payload = {
            "query": {
                "original": "what time does Cole's closed in east bell",
                "more_results_available": False,
            },
            "web": {
                "results": [
                    {
                        "title": "Cole Elementary - Bell Schedule",
                        "url": "https://school.example/bell-schedule",
                        "description": "School schedule.",
                    }
                ]
            },
        }
        suggest_payload = {
            "results": [
                {"query": "cole elementary bell schedule"},
                {"query": "east bell road construction"},
            ]
        }
        calls: list[str] = []

        def fake_read_json(request, *, timeout_val: int):
            path = urllib.parse.urlparse(request.full_url).path
            calls.append(path)
            return (suggest_payload, None) if path.endswith("/suggest/search") else (initial_payload, None)

        with patch.object(brave_search, "_read_json", side_effect=fake_read_json):
            result = brave_search.integration_web_search("what time does Cole's closed in east bell")

        self.assertEqual(len(calls), 2)
        self.assertTrue(result["autosuggest_attempted"])
        self.assertIsNone(result["suggested_query"])
        self.assertFalse(result["recovery_applied"])
        self.assertEqual(result["results"][0]["title"], "Cole Elementary - Bell Schedule")

    def test_autosuggest_is_skipped_when_initial_results_have_hours_signal(self) -> None:
        self.configure()
        payload = {
            "query": {"original": "when does kohls close in eastvale", "more_results_available": False},
            "web": {
                "results": [
                    {
                        "title": "Kohl's Eastvale Store Hours",
                        "url": "https://www.kohls.com/stores/ca/eastvale.shtml",
                        "description": "Opening hours and store information.",
                    }
                ]
            },
        }

        with patch.object(brave_search, "_read_json", return_value=(payload, None)) as read_json:
            result = brave_search.integration_web_search("when does kohls close in eastvale")

        self.assertEqual(read_json.call_count, 1)
        self.assertFalse(result["autosuggest_attempted"])
        self.assertFalse(result["recovery_applied"])

    def test_last_supported_page_does_not_offer_an_invalid_next_page(self) -> None:
        self.configure()
        payload = {
            "query": {"original": "tater", "more_results_available": True},
            "web": {
                "results": [
                    {"title": f"Result {index}", "url": f"https://example.com/{index}"}
                    for index in range(10)
                ]
            },
        }
        observed: dict[str, list[str]] = {}

        def fake_read_json(request, *, timeout_val: int):
            observed.update(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query))
            return payload, None

        with patch.object(brave_search, "_read_json", side_effect=fake_read_json):
            result = brave_search.integration_web_search("tater", num_results=10, start=91)

        self.assertEqual(observed["offset"], ["9"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_start"])

    def test_missing_key_and_api_error_use_search_contract(self) -> None:
        missing = brave_search.integration_web_search("tater")
        self.assertFalse(missing["ok"])
        self.assertIn("API Key", missing["needs"][0])

        self.configure()
        with patch.object(
            brave_search,
            "_read_json",
            return_value=({"error": {"detail": "Rate limit reached", "code": "RATE_LIMITED"}}, None),
        ):
            failed = brave_search.integration_web_search("tater")

        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "Rate limit reached")

    def test_test_action_returns_normalized_success(self) -> None:
        with patch.object(
            brave_search,
            "integration_web_search",
            return_value={"ok": True, "count": 1},
        ):
            result = brave_search.run_integration_action("test", {"query": "tater"})

        self.assertEqual(
            result,
            {"ok": True, "message": "Brave Search returned a result.", "count": 1},
        )


if __name__ == "__main__":
    unittest.main()
