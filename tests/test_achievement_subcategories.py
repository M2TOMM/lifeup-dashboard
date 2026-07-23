from pathlib import Path

from fixtures import LocalCrudApiTestCase


class AchievementSubcategoryApiTests(LocalCrudApiTestCase):
    def seed_hierarchy(self):
        connection = self.connect()
        try:
            connection.executemany(
                """
                INSERT INTO userachievementmodel (
                    id, content, description, type, categoryid, rewardcoin, expreward,
                    icon, achievementstatus, currentvalue, progress, createtime,
                    updatetime, isdelete, isgotreward, rewardcoinvariable, orderincategory
                ) VALUES (?, ?, '', ?, 7, 0, 0, '', 0, 0, 0, 1000, 1000, 0, 0, 0, ?)
                """,
                [
                    (90, "分类外成就", 0, 0),
                    (100, "第一阶段", 1, 10),
                    (101, "阶段一成就", 0, 11),
                    (102, "第二阶段", 1, 20),
                    (103, "阶段二成就", 0, 21),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def test_list_marks_subcategory_headers_and_children(self):
        self.seed_hierarchy()

        response = self.client.get("/api/achievements?category_id=7")
        self.assertEqual(response.status_code, 200, response.get_json())
        rows = {row["id"]: row for row in response.get_json()}

        self.assertEqual(rows[90]["record_kind"], "achievement")
        self.assertIsNone(rows[90]["subcategory_id"])
        self.assertEqual(rows[100]["record_kind"], "subcategory")
        self.assertEqual(rows[100]["subcategory_child_count"], 1)
        self.assertEqual(rows[101]["subcategory_id"], 100)
        self.assertEqual(rows[101]["subcategory_name"], "第一阶段")
        self.assertEqual(rows[103]["subcategory_id"], 102)

    def test_search_keeps_matching_child_subcategory_header(self):
        self.seed_hierarchy()

        response = self.client.get("/api/achievements?category_id=7&search=阶段一成就")
        self.assertEqual(response.status_code, 200, response.get_json())

        self.assertEqual([row["id"] for row in response.get_json()], [100, 101])

    def test_create_subcategory_and_place_achievement_inside_it(self):
        self.seed_hierarchy()

        subcategory = self.post_json(
            "/api/achievements/add",
            {"name": "第三阶段", "type": 1, "category_id": 7},
        )
        achievement = self.post_json(
            "/api/achievements/add",
            {
                "name": "阶段一新增成就",
                "type": 0,
                "category_id": 7,
                "subcategory_id": 100,
                "coin": 12,
                "exp": 34,
            },
        )

        response = self.client.get("/api/achievements?category_id=7")
        rows = response.get_json()
        ids = [row["id"] for row in rows]
        self.assertLess(ids.index(101), ids.index(achievement["id"]))
        self.assertLess(ids.index(achievement["id"]), ids.index(102))
        self.assertEqual(ids[-1], subcategory["id"])
        created = next(row for row in rows if row["id"] == achievement["id"])
        self.assertEqual(created["subcategory_id"], 100)

    def test_update_moves_achievement_to_another_subcategory(self):
        self.seed_hierarchy()

        self.post_json(
            "/api/achievements/update",
            {
                "id": 101,
                "name": "阶段一成就",
                "type": 0,
                "category_id": 7,
                "subcategory_id": 102,
                "coin": 0,
                "exp": 0,
                "icon": "",
                "description": "",
            },
        )

        response = self.client.get("/api/achievements?category_id=7")
        rows = {row["id"]: row for row in response.get_json()}
        self.assertEqual(rows[101]["subcategory_id"], 102)
        self.assertEqual(rows[100]["subcategory_child_count"], 0)
        self.assertEqual(rows[102]["subcategory_child_count"], 2)

    def test_non_empty_subcategory_cannot_be_deleted(self):
        self.seed_hierarchy()

        connection = self.connect()
        try:
            before = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM userachievementmodel ORDER BY id"
                ).fetchall()
            ]
        finally:
            connection.close()

        response = self.client.post("/api/achievements/delete", json={"id": 100})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["code"], "ACHIEVEMENT_SUBCATEGORY_NOT_EMPTY")
        self.assertEqual(response.get_json()["child_count"], 1)

        connection = self.connect()
        try:
            after = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM userachievementmodel ORDER BY id"
                ).fetchall()
            ]
        finally:
            connection.close()
        self.assertEqual(after, before)

    def test_non_empty_subcategory_cannot_change_type_or_main_category(self):
        self.seed_hierarchy()

        def hierarchy_snapshot():
            connection = self.connect()
            try:
                return [
                    tuple(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM userachievementmodel
                        WHERE categoryid IN (7, 8)
                        ORDER BY id
                        """
                    ).fetchall()
                ]
            finally:
                connection.close()

        blocked_updates = [
            {"id": 100, "type": 0},
            {"id": "100", "type": 0},
            {"id": 100, "category_id": 8},
            {"id": "100", "category_id": 8},
        ]
        for payload in blocked_updates:
            with self.subTest(payload=payload):
                before = hierarchy_snapshot()
                response = self.client.post("/api/achievements/update", json=payload)
                self.assertEqual(response.status_code, 409, response.get_json())
                self.assertEqual(
                    response.get_json()["code"],
                    "ACHIEVEMENT_SUBCATEGORY_NOT_EMPTY",
                )
                self.assertEqual(response.get_json()["child_count"], 1)
                self.assertEqual(hierarchy_snapshot(), before)

        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT type, categoryid FROM userachievementmodel WHERE id=100"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(tuple(row), (1, 7))

    def test_partial_achievement_update_preserves_subcategory_until_explicitly_cleared(self):
        self.seed_hierarchy()

        self.post_json(
            "/api/achievements/update",
            {"id": 101, "name": "只修改名称"},
        )
        rows = {
            row["id"]: row
            for row in self.client.get("/api/achievements?category_id=7").get_json()
        }
        self.assertEqual(rows[101]["name"], "只修改名称")
        self.assertEqual(rows[101]["subcategory_id"], 100)

        self.post_json(
            "/api/achievements/update",
            {"id": 101, "subcategory_id": None},
        )
        rows = {
            row["id"]: row
            for row in self.client.get("/api/achievements?category_id=7").get_json()
        }
        self.assertIsNone(rows[101]["subcategory_id"])


class AchievementSubcategoryUiTests(LocalCrudApiTestCase):
    def test_frontend_exposes_main_category_and_subcategory_controls(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("大类：", html)
        self.assertIn("achievement-kind-toggle", html)
        self.assertIn("setAchievementFormType", html)
        self.assertIn("refreshAchievementSubcategoryOptions", html)
        self.assertIn("toggleAchievementSubcategory", html)
        self.assertIn("var collapsed = !search &&", html)
        self.assertIn("var childStyle = a.subcategory_id && !search &&", html)
        self.assertIn("achievementFormOriginalType", html)
        self.assertIn("achievementFormTypeChanged", html)
