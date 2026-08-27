import unittest
from unittest.mock import AsyncMock

from mediabot.providers.sonarr import SonarrError, SonarrProvider


class SonarrProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_provider_is_disabled(self):
        provider = SonarrProvider(base_url="http://sonarr.test", api_key="")
        await provider.start()
        self.assertFalse(provider.enabled)
        with self.assertRaisesRegex(SonarrError, "not configured"):
            await provider.health()

    async def test_inventory_distinguishes_files_queue_and_missing(self):
        provider = SonarrProvider(
            base_url="http://sonarr.test",
            api_key="test-key",
        )

        async def fake_request(method, path, **kwargs):
            if path == "/series":
                return [{"id": 10, "tvdbId": 123, "seasons": []}]
            if path == "/episode":
                return [
                    {
                        "id": 1,
                        "seasonNumber": 7,
                        "episodeNumber": 1,
                        "hasFile": True,
                        "airDateUtc": "2020-01-01T00:00:00Z",
                    },
                    {
                        "id": 2,
                        "seasonNumber": 7,
                        "episodeNumber": 2,
                        "hasFile": False,
                        "monitored": False,
                        "airDateUtc": "2020-01-02T00:00:00Z",
                    },
                    {
                        "id": 3,
                        "seasonNumber": 7,
                        "episodeNumber": 3,
                        "hasFile": False,
                        "monitored": True,
                        "airDateUtc": "2099-01-01T00:00:00Z",
                    },
                ]
            if path == "/queue":
                return {
                    "records": [{
                        "episodeId": 2,
                        "status": "downloading",
                        "trackedDownloadStatus": "warning",
                        "trackedDownloadState": "importBlocked",
                        "statusMessages": [{
                            "title": "Import failed",
                            "messages": ["No files found are eligible for import"],
                        }],
                    }],
                }
            raise AssertionError(path)

        provider.request = AsyncMock(side_effect=fake_request)
        inventory = await provider.series_inventory(123)
        state = inventory["seasons"][7]

        self.assertEqual(state["available"], {1})
        self.assertEqual(state["missing"], {2, 3})
        self.assertEqual(state["queued"], {2})
        self.assertEqual(state["future"], {3})
        self.assertEqual(state["monitored"], {3})
        self.assertEqual(state["queue_status"], {2: "warning: import blocked"})
        self.assertEqual(
            state["queue_details"][2]["tracked_download_status"],
            "warning",
        )
        self.assertEqual(
            state["queue_details"][2]["tracked_download_state"],
            "importBlocked",
        )

    async def test_request_monitors_exact_ids_and_searches_only_aired_episodes(self):
        provider = SonarrProvider(
            base_url="http://sonarr.test",
            api_key="test-key",
        )
        provider.series_inventory = AsyncMock(return_value={
            "series": {
                "id": 10,
                "seasons": [{"seasonNumber": 7, "monitored": False}],
            },
            "episodes": [
                {
                    "id": 2,
                    "seasonNumber": 7,
                    "episodeNumber": 2,
                    "hasFile": False,
                    "airDateUtc": "2020-01-02T00:00:00Z",
                },
                {
                    "id": 3,
                    "seasonNumber": 7,
                    "episodeNumber": 3,
                    "hasFile": False,
                    "airDateUtc": "2099-01-01T00:00:00Z",
                },
            ],
            "seasons": {},
        })

        async def fake_request(method, path, **kwargs):
            if path == "/command":
                return {"id": 77}
            return {}

        provider.request = AsyncMock(side_effect=fake_request)
        result = await provider.request_missing_episodes(
            tvdb_id=123,
            missing_by_season={7: {2, 3}},
        )

        self.assertEqual(result["future_count"], 1)
        self.assertEqual(result["command_id"], 77)
        self.assertEqual(result["accepted_by_season"], {7: [2, 3]})
        self.assertFalse(result["partial"])
        self.assertTrue(result["monitor_succeeded"])
        self.assertTrue(result["search_succeeded"])
        calls = provider.request.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[:2], ("PUT", "/episode/monitor"))
        self.assertEqual(
            calls[0].kwargs["json"],
            {"episodeIds": [2, 3], "monitored": True},
        )
        self.assertEqual(calls[1].args[:2], ("POST", "/command"))
        self.assertEqual(
            calls[1].kwargs["json"],
            {"name": "EpisodeSearch", "episodeIds": [2]},
        )

    async def test_search_failure_reports_partial_after_exact_monitor(self):
        provider = SonarrProvider(
            base_url="http://sonarr.test",
            api_key="test-key",
        )
        provider.series_inventory = AsyncMock(return_value={
            "series": {"id": 10},
            "episodes": [{
                "id": 2,
                "seasonNumber": 7,
                "episodeNumber": 2,
                "hasFile": False,
                "airDateUtc": "2020-01-02T00:00:00Z",
            }],
            "seasons": {},
        })

        async def fake_request(method, path, **kwargs):
            if path == "/episode/monitor":
                return {}
            if path == "/command":
                raise SonarrError("Sonarr HTTP 500: search failed")
            raise AssertionError(path)

        provider.request = AsyncMock(side_effect=fake_request)
        result = await provider.request_missing_episodes(
            tvdb_id=123,
            missing_by_season={7: {2}},
        )

        self.assertEqual(result["outcome"], "partial")
        self.assertTrue(result["partial"])
        self.assertTrue(result["monitor_succeeded"])
        self.assertFalse(result["search_succeeded"])
        self.assertEqual(result["monitored_episode_ids"], [2])
        self.assertEqual(result["searched_episode_ids"], [])
        self.assertEqual(result["search_failed_episode_ids"], [2])
        self.assertIn("search failed", result["search_error"])

    async def test_raced_available_episodes_return_clean_noop(self):
        provider = SonarrProvider(
            base_url="http://sonarr.test",
            api_key="test-key",
        )
        provider.series_inventory = AsyncMock(return_value={
            "series": {"id": 10},
            "episodes": [{
                "id": 2,
                "seasonNumber": 7,
                "episodeNumber": 2,
                "hasFile": True,
                "airDateUtc": "2020-01-02T00:00:00Z",
            }],
            "seasons": {},
        })
        provider.request = AsyncMock()

        result = await provider.request_missing_episodes(
            tvdb_id=123,
            missing_by_season={7: {2}},
        )

        self.assertEqual(result["outcome"], "already_available")
        self.assertTrue(result["already_available"])
        self.assertEqual(result["accepted_by_season"], {})
        self.assertEqual(result["already_available_by_season"], {7: [2]})
        provider.request.assert_not_awaited()

    async def test_accepted_mapping_excludes_raced_and_unresolved_episodes(self):
        provider = SonarrProvider(
            base_url="http://sonarr.test",
            api_key="test-key",
        )
        provider.series_inventory = AsyncMock(return_value={
            "series": {"id": 10},
            "episodes": [
                {
                    "id": 2,
                    "seasonNumber": 7,
                    "episodeNumber": 2,
                    "hasFile": False,
                    "airDateUtc": "2020-01-02T00:00:00Z",
                },
                {
                    "id": 3,
                    "seasonNumber": 7,
                    "episodeNumber": 3,
                    "hasFile": True,
                    "airDateUtc": "2020-01-03T00:00:00Z",
                },
            ],
            "seasons": {},
        })

        async def fake_request(method, path, **kwargs):
            if path == "/episode/monitor":
                return {}
            if path == "/command":
                return {"id": 77}
            raise AssertionError(path)

        provider.request = AsyncMock(side_effect=fake_request)
        result = await provider.request_missing_episodes(
            tvdb_id=123,
            missing_by_season={7: {2, 3, 4}},
        )

        self.assertEqual(result["accepted_by_season"], {7: [2]})
        self.assertEqual(result["already_available_by_season"], {7: [3]})
        self.assertEqual(result["unresolved"], {7: [4]})
        self.assertEqual(result["monitored_episode_ids"], [2])
        self.assertEqual(
            provider.request.await_args_list[0].kwargs["json"],
            {"episodeIds": [2], "monitored": True},
        )


if __name__ == "__main__":
    unittest.main()
