from __future__ import annotations

import unittest
from unittest.mock import patch

from app.local_server import (
    DEFAULT_LOCAL_DISPLAY_NAME,
    _resolve_display_name,
    get_local_profile,
)


class DisplayNameResolutionTest(unittest.TestCase):
    def test_uses_declared_priority_instead_of_mongo_result_order(self) -> None:
        documents = [
            {"attribute_key": "称呼", "attribute_value": "小王"},
            {"attribute_key": "name", "attribute_value": "William"},
            {"attribute_key": "姓名", "attribute_value": "王小明"},
        ]

        self.assertEqual(_resolve_display_name(documents), "王小明")

    def test_skips_blank_or_non_string_values_and_uses_next_key(self) -> None:
        documents = [
            {"attribute_key": "姓名", "attribute_value": "  "},
            {"attribute_key": "name", "attribute_value": {"value": "错误结构"}},
            {"attribute_key": "称呼", "attribute_value": " 老王 "},
        ]

        self.assertEqual(_resolve_display_name(documents), "老王")

    def test_defaults_to_laoji(self) -> None:
        self.assertEqual(_resolve_display_name([]), DEFAULT_LOCAL_DISPLAY_NAME)


class _FakeCursor:
    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    async def to_list(self, length: int) -> list[dict]:
        return self._documents[:length]


class _FakeCollection:
    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    def find(self, query: dict, projection: dict) -> _FakeCursor:
        assert query == {"attribute_key": {"$in": ["姓名", "name", "称呼"]}}
        assert projection == {"_id": 0, "attribute_key": 1, "attribute_value": 1}
        return _FakeCursor(self._documents)


class _FakeMongoClient:
    def __init__(self, documents: list[dict]) -> None:
        self.profile_attributes = _FakeCollection(documents)


class LocalProfileEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_profile_attribute_collection(self) -> None:
        client = _FakeMongoClient([
            {"attribute_key": "称呼", "attribute_value": "老己同学"},
        ])
        with patch("app.local_server.get_mongo_memory_client", return_value=client):
            self.assertEqual(await get_local_profile(), {"display_name": "老己同学"})

    async def test_mongo_failure_keeps_frontend_usable(self) -> None:
        with patch("app.local_server.get_mongo_memory_client", side_effect=RuntimeError("offline")):
            self.assertEqual(await get_local_profile(), {"display_name": "老己"})


if __name__ == "__main__":
    unittest.main()
