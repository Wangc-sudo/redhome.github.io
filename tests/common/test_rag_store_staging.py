# -*- coding: utf-8 -*-

import re
import unittest

try:
    import pymilvus  # noqa: F401
except ModuleNotFoundError:
    raise unittest.SkipTest("pymilvus is required for Milvus store contract tests")

from common.rag import store


class _Milvus:
    def __init__(self):
        self.rows = {}
        self.fail_production = False

    def load_collection(self, _collection):
        return None

    def upsert(self, _collection, rows):
        if self.fail_production and any(not row["id"].startswith("__stage__:") for row in rows):
            self.fail_production = False
            raise RuntimeError("upsert failed")
        for row in rows:
            self.rows[row["id"]] = dict(row)
        return {"upsert_count": len(rows)}

    def delete(self, _collection, ids):
        for item_id in ids:
            self.rows.pop(item_id, None)
        return {"delete_count": len(ids)}

    def query(self, _collection, *, filter, output_fields, limit, offset=0):
        ids = re.findall(r'"([^"]+)"', filter)
        rows = [self.rows[item_id] for item_id in ids if item_id in self.rows]
        return [
            {key: row.get(key) for key in output_fields}
            for row in rows[offset:offset + limit]
        ]

    def search(self, _collection, _vectors, *, limit, filter, output_fields):
        rows = list(self.rows.values())
        if filter == 'kind != "staging"':
            rows = [row for row in rows if row["kind"] != "staging"]
        elif filter and filter.startswith('kind == "'):
            kind = filter.split('"')[1]
            rows = [row for row in rows if row["kind"] == kind]
        return [[{
            "id": row["id"], "distance": 1.0,
            "entity": {key: row.get(key) for key in output_fields},
        } for row in rows[:limit]]]


def _item(name="new"):
    return {
        "id": "product:r1", "vector": [0.1, 0.2], "kind": "text",
        "name": name, "text": name, "image_ref": "",
    }


class StagingStoreTests(unittest.TestCase):
    def test_staging_is_hidden_until_verified_publish(self):
        client = _Milvus()
        client.rows["product:r1"] = _item("old")
        staged = store.stage_items(client, [_item()], run_id="run1")
        self.assertEqual("old", client.rows["product:r1"]["name"])
        self.assertNotIn(staged[0]["staged_id"], {
            hit["id"] for hit in store.search(client, [0.1, 0.2])
        })
        self.assertTrue(store.verify_staged_items(client, staged, dim=2))
        self.assertEqual(1, store.publish_staged_items(client, staged))
        self.assertEqual("new", client.rows["product:r1"]["name"])
        self.assertFalse(any(item_id.startswith("__stage__:") for item_id in client.rows))

    def test_missing_staged_row_never_touches_last_good(self):
        client = _Milvus()
        client.rows["product:r1"] = _item("old")
        staged = store.stage_items(client, [_item()], run_id="run1")
        client.rows.pop(staged[0]["staged_id"])
        with self.assertRaisesRegex(RuntimeError, "staging_count_mismatch"):
            store.verify_staged_items(client, staged, dim=2)
        self.assertEqual("old", client.rows["product:r1"]["name"])

    def test_publish_failure_preserves_last_good_and_discards_staging(self):
        client = _Milvus()
        client.rows["product:r1"] = _item("old")
        staged = store.stage_items(client, [_item()], run_id="run1")
        store.verify_staged_items(client, staged, dim=2)
        client.fail_production = True
        with self.assertRaisesRegex(RuntimeError, "upsert failed"):
            store.publish_staged_items(client, staged)
        self.assertEqual("old", client.rows["product:r1"]["name"])
        store.discard_staged_items(client, staged)
        self.assertNotIn(staged[0]["staged_id"], client.rows)


if __name__ == "__main__":
    unittest.main()
