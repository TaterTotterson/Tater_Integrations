from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

helpers = types.ModuleType("helpers")
helpers.redis_client = None
sys.modules.setdefault("helpers", helpers)

from integrations import duckduckgo_search


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, *, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)


class FakeDDGS:
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.init_kwargs = dict(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def text(self, query: str, **kwargs):
        self.calls.append({"query": query, "init": self.init_kwargs, **kwargs})
        return [
            {
                "title": f"Result {index}",
                "href": f"https://example{index}.com/page",
                "body": f"Snippet {index}",
            }
            for index in range(1, 8)
        ]


class DuckDuckGoSearchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        duckduckgo_search.redis_client = self.redis
        FakeDDGS.calls = []

    def test_settings_round_trip_and_region_validation(self) -> None:
        saved = duckduckgo_search.save_duckduckgo_search_settings(region="US_EN")
        self.assertEqual(saved["DUCKDUCKGO_REGION"], "us-en")

        saved = duckduckgo_search.save_duckduckgo_search_settings(region="not-a-region")
        self.assertEqual(saved["DUCKDUCKGO_REGION"], "wt-wt")

    def test_search_uses_contract_and_paginates_results(self) -> None:
        self.redis.hashes[duckduckgo_search.DUCKDUCKGO_SEARCH_SETTINGS_KEY] = {
            "DUCKDUCKGO_REGION": "wt-wt",
        }
        with (
            patch.object(duckduckgo_search, "_load_ddgs_client", return_value=(FakeDDGS, "ddgs")),
            patch.object(duckduckgo_search, "_proxy_url", return_value=None),
        ):
            result = duckduckgo_search.integration_web_search(
                "coffee",
                num_results=2,
                start=3,
                site="example.com",
                safe="off",
                country="gb",
                language="en",
                timeout_sec=9,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "duckduckgo_search")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["title"], "Result 3")
        self.assertEqual(result["next_start"], 5)
        self.assertEqual(result["site_filter"], "example.com")
        call = FakeDDGS.calls[0]
        self.assertEqual(call["query"], "site:example.com coffee")
        self.assertEqual(call["region"], "uk-en")
        self.assertEqual(call["safesearch"], "off")
        self.assertEqual(call["backend"], "duckduckgo")
        self.assertEqual(call["init"]["timeout"], 9)

    def test_legacy_client_uses_compatible_backend(self) -> None:
        with (
            patch.object(
                duckduckgo_search,
                "_load_ddgs_client",
                return_value=(FakeDDGS, "duckduckgo_search"),
            ),
            patch.object(duckduckgo_search, "_proxy_url", return_value=None),
        ):
            result = duckduckgo_search.integration_web_search("tater", num_results=1)

        self.assertTrue(result["ok"])
        self.assertEqual(FakeDDGS.calls[0]["backend"], "auto")

    def test_missing_dependency_is_reported_without_import_failure(self) -> None:
        with patch.object(
            duckduckgo_search,
            "_load_ddgs_client",
            side_effect=RuntimeError("missing"),
        ):
            status = duckduckgo_search.integration_status()
            result = duckduckgo_search.integration_web_search("tater")

        self.assertFalse(status["configured"])
        self.assertFalse(result["ok"])
        self.assertIn("Install the ddgs Python package", result["needs"][0])

    def test_test_action_returns_normalized_success(self) -> None:
        with patch.object(
            duckduckgo_search,
            "integration_web_search",
            return_value={"ok": True, "count": 1},
        ):
            result = duckduckgo_search.run_integration_action("test", {"query": "tater"})

        self.assertEqual(
            result,
            {"ok": True, "message": "DuckDuckGo Search returned a result.", "count": 1},
        )


if __name__ == "__main__":
    unittest.main()
