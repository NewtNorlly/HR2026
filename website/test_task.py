import unittest
from unittest.mock import patch

import test_app
from app import app, db, one


class TaskListTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.SiteTests()
        self.fixture.setUp()
        self.client = self.fixture.client

    def login_as(self, member_id):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(uid=member_id, version=0, csrf="test-session-csrf")

    def save(self, title, *, owner, reviewer, parent=None, record=None, week=13):
        data = {
            "title": title,
            "owner": owner,
            "reviewer": reviewer,
            "due": "2026-11-22",
            "deliverable": "提交排版验收",
            "week": week,
            "csrf": "test-session-csrf",
        }
        if parent is not None:
            data["parent"] = parent
        if record is not None:
            data["id"] = record
        return self.client.post("/task/save", data=data)

    def progress(self, record, status):
        return self.client.post(f"/task/{record}/progress", data={
            "csrf": "test-session-csrf", "status": status, "risk": "green",
        })

    def test_public_group_order_and_nested_tasks(self):
        self.login_as(1)
        self.assertEqual(self.save("Officer 细则", owner=1, reviewer=1, parent=1).status_code, 302)
        self.login_as(2)
        self.assertEqual(self.save("Group 1 纲要", owner=5, reviewer=2).status_code, 302)
        self.assertEqual(self.save("Group 1 细则", owner=5, reviewer=2, parent=3).status_code, 302)

        visitor = app.test_client()
        with patch("app.PUBLIC_ROSTER", False):
            data = visitor.get("/task/data").json
            lists = data["lists"]
            self.assertEqual([group["title"] for group in lists], ["Officer", *[f"Group {i}" for i in range(1, 7)]])
            self.assertEqual(lists[0]["outlines"][0]["children"][0]["title"], "Officer 细则")
            detail = lists[1]["outlines"][0]["children"][0]
            self.assertEqual(detail["assignee"], "成员 005")
            self.assertEqual(detail["due"], "2026-11-22")
            self.assertEqual(detail["status"], "todo")
            page = visitor.get("/task")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Group 1 细则".encode(), page.data)
            self.assertNotIn("示例联络员".encode(), page.data)
            self.assertNotIn(b'name="deliverable"', page.data)
        self.assertEqual(visitor.get("/task/data?week=18").json["lists"][0]["outlines"], [])

    def test_edits_stay_inside_user_list_and_reject_nested_cycles(self):
        self.login_as(2)
        self.assertEqual(self.save("跨往 Officer", owner=5, reviewer=2, parent=1).status_code, 403)
        self.assertEqual(self.save("编辑 Officer 条目", owner=5, reviewer=2, record=1).status_code, 403)
        self.assertEqual(self.save("Group 1 纲要", owner=5, reviewer=2).status_code, 302)
        self.assertEqual(self.save("Group 1 细则", owner=5, reviewer=2, parent=2).status_code, 302)
        self.assertEqual(self.save("细则下继续嵌套", owner=5, reviewer=2, parent=3).status_code, 400)
        self.assertEqual(self.save("循环", owner=5, reviewer=2, record=2, parent=2).status_code, 400)

        self.login_as(1)
        self.assertEqual(self.save("主编不可改组内", owner=1, reviewer=1, record=2).status_code, 403)
        self.assertEqual(self.save("Officer 细则不能挂小组", owner=1, reviewer=1, parent=2).status_code, 403)
        self.login_as(5)
        self.assertEqual(self.save("组员不可改纲要", owner=5, reviewer=2, record=2).status_code, 403)
        self.assertEqual(self.progress(3, "doing").status_code, 302)
        self.assertEqual(self.progress(3, "done").status_code, 403)
        self.login_as(2)
        self.assertEqual(self.progress(3, "done").status_code, 302)

    def test_grandfathered_officer_dependency(self):
        with app.app_context(), db():
            db().execute("""INSERT INTO tasks(id,title,week,group_id,parent_id,owner_id,reviewer_id,due,deliverable)
                          VALUES(2,'历史小组细则',13,1,1,5,2,'2026-11-22','保留原关联')""")
        lists = app.test_client().get("/task/data").json["lists"]
        self.assertEqual(lists[0]["outlines"][0]["children"], [])
        self.assertEqual(lists[1]["outlines"][0]["id"], 2)
        self.login_as(1)
        self.assertEqual(self.progress(1, "doing").status_code, 302)
        self.assertEqual(self.progress(1, "done").status_code, 409)
        self.login_as(2)
        self.assertEqual(self.save("历史小组细则更新", owner=5, reviewer=2, parent=1, record=2).status_code, 302)
        with app.app_context():
            self.assertEqual(one("SELECT parent_id FROM tasks WHERE id=2")["parent_id"], 1)
        self.assertEqual(self.progress(2, "doing").status_code, 302)
        self.assertEqual(self.progress(2, "done").status_code, 302)
        self.login_as(1)
        self.assertEqual(self.progress(1, "done").status_code, 302)


if __name__ == "__main__":
    unittest.main()
