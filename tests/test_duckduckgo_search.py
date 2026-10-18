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


def result_markup(index: int) -> str:
    target = f"https://example{index}.com/page?item={index}"
    redirect = "//duckduckgo.com/l/?uddg=" + target.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F").replace("=", "%3D")
    return f"""
    <div class="result web-result">
      <h2><a class="result__a" href="{redirect}">Result <b>{index}</b></a></h2>
      <a class="result__snippet" href="{redirect}">Snippet <b>{index}</b></a>
    </div>
    """


def search_page(indices: list[int], *, next_offset: int | None = None) -> str:
    next_form = ""
    if next_offset is not None:
        next_form = f"""
        <div class="nav-link"><form action="/html/" method="post">
          <input type="submit" value="Next" />
          <input type="hidden" name="q" value="site:example.com coffee" />
          <input type="hidden" name="s" value="{next_offset}" />
          <input type="hidden" name="vqd" value="test-token" />
        </form></div>
        """
    return "<html><body>" + "".join(result_markup(index) for index in indices) + next_form + "</body></html>"


class DuckDuckGoSearchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        duckduckgo_search.redis_client = self.redis

    def test_settings_round_trip_and_region_validation(self) -> None:
        saved = duckduckgo_search.save_duckduckgo_search_settings(region="US_EN")
        self.assertEqual(saved["DUCKDUCKGO_REGION"], "us-en")

        saved = duckduckgo_search.save_duckduckgo_search_settings(region="not-a-region")
        self.assertEqual(saved["DUCKDUCKGO_REGION"], "wt-wt")

    def test_search_uses_contract_and_paginates_results(self) -> None:
        self.redis.hashes[duckduckgo_search.DUCKDUCKGO_SEARCH_SETTINGS_KEY] = {
            "DUCKDUCKGO_REGION": "wt-wt",
        }
        calls: list[dict] = []

        def fake_fetch(payload: dict[str, str], *, timeout_sec: int) -> str:
            calls.append({"payload": dict(payload), "timeout_sec": timeout_sec})
            return search_page([1, 2], next_offset=2) if len(calls) == 1 else search_page([3, 4, 5])

        with patch.object(duckduckgo_search, "_fetch_search_page", side_effect=fake_fetch):
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
        self.assertEqual(result["results"][0]["url"], "https://example3.com/page?item=3")
        self.assertEqual(calls[0]["payload"]["q"], "site:example.com coffee")
        self.assertEqual(calls[0]["payload"]["kl"], "uk-en")
        self.assertEqual(calls[0]["payload"]["kp"], "-2")
        self.assertEqual(calls[0]["timeout_sec"], 9)
        self.assertEqual(calls[1]["payload"]["s"], "2")
        self.assertEqual(calls[1]["payload"]["kl"], "uk-en")

    def test_status_is_ready_without_an_external_dependency(self) -> None:
        self.assertEqual(
            duckduckgo_search.integration_status(),
            {
                "configured": True,
                "message": "DuckDuckGo Search is ready. No API key or additional package is required.",
            },
        )

    def test_upstream_failure_is_returned_as_search_error(self) -> None:
        with patch.object(
            duckduckgo_search,
            "_fetch_search_page",
            side_effect=RuntimeError("DuckDuckGo returned HTTP 429."),
        ):
            result = duckduckgo_search.integration_web_search("tater")

        self.assertFalse(result["ok"])
        self.assertIn("HTTP 429", result["error"])

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
