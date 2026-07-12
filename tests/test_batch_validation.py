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
                    isfrozen INTEGER NOT NULL,
                    groupid INTEGER NOT NULL
                );
                CREATE TABLE shopitemmodel (
                    id INTEGER PRIMARY KEY,
                    price INTEGER NOT NULL,
                    isdisablepurchase INTEGER NOT NULL,
                    isdel INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO taskmodel
                    (id, taskstatus, updatedtime, isdeleterecord, isfrozen, groupid)
                VALUES (?, 0, 100, 0, 0, ?)
                """,
                [(1, 10), (2, 10), (3, 20)],
            )
            conn.executemany(
                """
                INSERT INTO shopitemmodel
                    (id, price, isdisablepurchase, isdel)
                VALUES (?, 100, 1, 0)
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
                SELECT id, price, isdisablepurchase, isdel
                FROM shopitemmodel ORDER BY id
                """
            ).fetchall()
        finally:
            conn.close()

    def execute_sql(self, sql, parameters=()):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(sql, parameters)
            conn.commit()
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

    def test_task_batch_rejects_missing_target_before_writing(self):
        self.assert_rejected_without_changes(
            "/api/tasks/batch",
            {"ids": [1, 999], "action": "freeze"},
            self.task_rows,
        )

    def test_item_batch_rejects_missing_target_before_writing(self):
        self.assert_rejected_without_changes(
            "/api/items/batch",
            {"ids": [1, 999], "action": "price", "price": 250},
            self.item_rows,
        )

    def test_task_batch_rolls_back_when_database_fails_mid_update(self):
        self.execute_sql(
            """
            CREATE TRIGGER fail_second_task_update
            BEFORE UPDATE ON taskmodel
            WHEN OLD.id = 2
            BEGIN
                SELECT RAISE(ABORT, 'forced task batch failure');
            END
            """
        )
        before = self.task_rows()

        response = self.client.post(
            "/api/tasks/batch", json={"ids": [1, 2], "action": "freeze"}
        )

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(self.task_rows(), before)

    def test_item_batch_rolls_back_when_database_fails_mid_update(self):
        self.execute_sql(
            """
            CREATE TRIGGER fail_second_item_update
            BEFORE UPDATE ON shopitemmodel
            WHEN OLD.id = 2
            BEGIN
                SELECT RAISE(ABORT, 'forced item batch failure');
            END
            """
        )
        before = self.item_rows()

        response = self.client.post(
            "/api/items/batch",
            json={"ids": [1, 2], "action": "price", "price": 250},
        )

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(self.item_rows(), before)

    def test_freeze_batch_rejects_invalid_state_and_ids(self):
        invalid_payloads = (
            {"ids": [1], "isfrozen": 1},
            {"ids": [1, 1], "isfrozen": True},
            {"ids": ["1"], "isfrozen": True},
            {"ids": list(range(1, MAX_BATCH_SIZE + 2)), "isfrozen": True},
            {"ids": [1, 999], "isfrozen": True},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assert_rejected_without_changes(
                    "/api/tasks/batch/freeze", payload, self.task_rows
                )

    def test_freeze_batch_updates_valid_ids(self):
        response = self.client.post(
            "/api/tasks/batch/freeze", json={"ids": [1, 2], "isfrozen": True}
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["affected"], 2)
        self.assertEqual([row[4] for row in self.task_rows()], [1, 1, 0])

    def test_freeze_batch_updates_valid_group(self):
        response = self.client.post(
            "/api/tasks/batch/freeze", json={"groupid": 10, "isfrozen": True}
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["affected"], 2)
        self.assertEqual([row[4] for row in self.task_rows()], [1, 1, 0])

    def test_task_batch_supported_actions_update_expected_fields(self):
        cases = (
            ("disable", "taskstatus", 1, 0),
            ("enable", "taskstatus", 0, 1),
            ("delete", "isdeleterecord", 0, 1),
            ("freeze", "isfrozen", 0, 1),
            ("unfreeze", "isfrozen", 1, 0),
        )
        for action, column, before_value, expected in cases:
            with self.subTest(action=action):
                self.execute_sql(
                    f"UPDATE taskmodel SET {column}=? WHERE id=1", (before_value,)
                )
                response = self.client.post(
                    "/api/tasks/batch", json={"ids": [1], "action": action}
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()["affected"], 1)
                column_index = {
                    "taskstatus": 1,
                    "isdeleterecord": 3,
                    "isfrozen": 4,
                }[column]
                self.assertEqual(self.task_rows()[0][column_index], expected)

    def test_item_batch_supported_actions_update_expected_fields(self):
        cases = (
            ("disable", "isdisablepurchase", 0, 1),
            ("enable", "isdisablepurchase", 1, 0),
            ("delete", "isdel", 0, 1),
        )
        for action, column, before_value, expected in cases:
            with self.subTest(action=action):
                self.execute_sql(
                    f"UPDATE shopitemmodel SET {column}=? WHERE id=1", (before_value,)
                )
                response = self.client.post(
                    "/api/items/batch", json={"ids": [1], "action": action}
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()["affected"], 1)
                column_index = {"isdisablepurchase": 2, "isdel": 3}[column]
                self.assertEqual(self.item_rows()[0][column_index], expected)


if __name__ == "__main__":
    unittest.main()
