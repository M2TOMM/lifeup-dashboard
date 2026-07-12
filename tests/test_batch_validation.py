import os
import shutil
import sqlite3
import tempfile
import unittest

import server


MAX_BATCH_SIZE = 200
MAX_ITEM_PRICE = 2_147_483_647


class BatchValidationTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self._tmpdir = tempfile.mkdtemp(prefix="lifeup-batch-validation-")
        self._db_path = os.path.join(self._tmpdir, "LifeUpDB.db")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE taskmodel (
                    id INTEGER PRIMARY KEY,
                    taskstatus INTEGER NOT NULL,
                    updatedtime INTEGER NOT NULL,
                    isdeleterecord INTEGER NOT NULL,
                    isfrozen INTEGER NOT NULL
                );
                CREATE TABLE shopitemmodel (
                    id INTEGER PRIMARY KEY,
                    price INTEGER NOT NULL,
                    purchasable INTEGER NOT NULL,
                    updatetime INTEGER NOT NULL,
                    isdel INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO taskmodel
                    (id, taskstatus, updatedtime, isdeleterecord, isfrozen)
                VALUES (?, 0, 100, 0, 0)
                """,
                [(1,), (2,), (3,)],
            )
            conn.executemany(
                """
                INSERT INTO shopitemmodel
                    (id, price, purchasable, updatetime, isdel)
                VALUES (?, 100, 1, 100, 0)
                """,
                [(1,), (2,), (3,)],
            )
            conn.commit()
        finally:
            conn.close()

        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self._db_path,
                "tmpdir": self._tmpdir,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def task_rows(self):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(
                """
                SELECT id, taskstatus, updatedtime, isdeleterecord, isfrozen
                FROM taskmodel ORDER BY id
                """
            ).fetchall()
        finally:
            conn.close()

    def item_rows(self):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(
                """
                SELECT id, price, purchasable, updatetime, isdel
                FROM shopitemmodel ORDER BY id
                """
            ).fetchall()
        finally:
            conn.close()

    def assert_rejected_without_changes(self, path, payload, rows_reader):
        before = rows_reader()
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, 400, response.get_json())
        body = response.get_json()
        self.assertIsInstance(body.get("error"), str)
        self.assertTrue(body["error"].strip())
        self.assertEqual(rows_reader(), before)

    def test_task_batch_rejects_unknown_or_missing_action(self):
        for action in ("archive", None):
            with self.subTest(action=action):
                payload = {"ids": [1]}
                if action is not None:
                    payload["action"] = action
                self.assert_rejected_without_changes(
                    "/api/tasks/batch", payload, self.task_rows
                )

    def test_item_batch_rejects_unknown_or_missing_action(self):
        for action in ("archive", None):
            with self.subTest(action=action):
                payload = {"ids": [1]}
                if action is not None:
                    payload["action"] = action
                self.assert_rejected_without_changes(
                    "/api/items/batch", payload, self.item_rows
                )

    def test_task_batch_rejects_empty_duplicate_and_invalid_ids(self):
        invalid_ids = (
            [],
            [1, 1],
            ["1"],
            [1.0],
            [True],
            [0],
            [-1],
        )
        for ids in invalid_ids:
            with self.subTest(ids=ids):
                self.assert_rejected_without_changes(
                    "/api/tasks/batch",
                    {"ids": ids, "action": "disable"},
                    self.task_rows,
                )

    def test_item_batch_rejects_empty_duplicate_and_invalid_ids(self):
        invalid_ids = (
            [],
            [1, 1],
            ["1"],
            [1.0],
            [True],
            [0],
            [-1],
        )
        for ids in invalid_ids:
            with self.subTest(ids=ids):
                self.assert_rejected_without_changes(
                    "/api/items/batch",
                    {"ids": ids, "action": "disable"},
                    self.item_rows,
                )

    def test_task_batch_rejects_more_than_limit(self):
        self.assert_rejected_without_changes(
            "/api/tasks/batch",
            {"ids": list(range(1, MAX_BATCH_SIZE + 2)), "action": "disable"},
            self.task_rows,
        )

    def test_item_batch_rejects_more_than_limit(self):
        self.assert_rejected_without_changes(
            "/api/items/batch",
            {"ids": list(range(1, MAX_BATCH_SIZE + 2)), "action": "disable"},
            self.item_rows,
        )

    def test_item_batch_rejects_invalid_price_before_writing(self):
        invalid_prices = (None, True, "10", 1.5, -1, MAX_ITEM_PRICE + 1)
        for price in invalid_prices:
            with self.subTest(price=price):
                payload = {"ids": [1], "action": "price"}
                if price is not None:
                    payload["price"] = price
                self.assert_rejected_without_changes(
                    "/api/items/batch", payload, self.item_rows
                )

    def test_item_batch_accepts_price_boundaries(self):
        for price in (0, MAX_ITEM_PRICE):
            with self.subTest(price=price):
                response = self.client.post(
                    "/api/items/batch",
                    json={"ids": [1], "action": "price", "price": price},
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()["affected"], 1)
                self.assertEqual(self.item_rows()[0][1], price)


if __name__ == "__main__":
    unittest.main()
