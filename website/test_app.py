import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

_sandbox = tempfile.TemporaryDirectory(prefix="hr2026-tests-")
os.environ["HR2026_DATA"] = _sandbox.name
os.environ.pop("HR2026_PRODUCTION", None)

from openpyxl import Workbook
from PIL import Image
from app import DATA, app, db, init_db, MILESTONES
from werkzeug.security import generate_password_hash


class SiteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=64 * 1024 * 1024)
        self.client = app.test_client()
        self.temporary = "Initial-passphrase-2026"
        self.current = "Changed-passphrase-2026"
        temporary_hash = generate_password_hash(self.temporary, method="pbkdf2:sha256:10000")
        with app.app_context(), db():
            for table in ("roster", "profiles", "comments", "issues", "contributions", "links", "audit", "tasks", "files", "users", "login_limits"):
                db().execute(f"DELETE FROM {table}")
            for row in [
                (1, "editor", "示例主编", "editor", None, 0),
                (2, "lead", "示例组长", "lead", 1, 0),
                (3, "member", "示例组员", "member", 1, 1),
                (4, "outsider", "另组成员", "member", 2, 0),
                (5, "liaison", "示例联络员", "liaison", 1, 0),
                (6, "hr", "示例HR", "hr", None, 0),
            ]:
                db().execute("INSERT INTO users(id,username,name,role,group_id,must_change,password) VALUES(?,?,?,?,?,?,?)",
                             (*row, temporary_hash))
            db().execute("INSERT INTO tasks(id,title,week,owner_id,reviewer_id,due,deliverable) VALUES(1,?,13,1,1,'',?)",
                         MILESTONES[0])

    def login(self, account, client=None, password=None):
        client = client or self.client
        if self.token(client):
            client.post("/logout", data={"csrf": self.token(client)})
        client.get("/login")
        return self.post("/login", {"username": account, "password": password or self.temporary}, client)

    @staticmethod
    def token(client):
        with client.session_transaction() as session:
            return session.get("csrf")

    def post(self, path, data, client=None):
        client = client or self.client
        token = self.token(client)
        if token is None:
            client.get("/login", follow_redirects=True)
            token = self.token(client)
        return client.post(path, data={"csrf": token, **data})

    def upload(self, name, data, group=1, previous=None):
        fields = {"group": str(group), "document": (io.BytesIO(data), name)}
        if previous:
            fields["previous"] = str(previous)
        return self.post("/files/upload", fields)

    def test_public_home_survives_concurrent_reads(self):
        def check(index):
            with app.test_client() as client:
                response = client.get("/health" if index % 2 else "/")
                return response.status_code
        with ThreadPoolExecutor(max_workers=8) as threads:
            self.assertEqual(list(threads.map(check, range(48))), [200] * 48)
        self.assertEqual(self.client.get("/task").status_code, 200)
        self.assertIn("HR2026年刊项目规划".encode(), self.client.get("/").data)
        self.assertEqual(self.client.get("/").headers["X-Content-Type-Options"], "nosniff")

    def test_first_login_and_csrf(self):
        self.assertTrue(self.login("示例组员").location.endswith("/password"))
        self.assertTrue(self.client.get("/task").location.endswith("/password"))
        self.assertEqual(self.client.post("/password", data={"current": self.temporary}).status_code, 400)
        self.assertEqual(self.post("/password", {"current": self.temporary, "new": self.current, "confirm": self.current}).status_code, 302)
        for route in ("/task", "/files", "/issues", "/wiki", "/people", "/people/3", "/activity"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200, route)
        self.assertEqual(self.login("member").status_code, 200)
        self.assertEqual(self.login("member", password=self.current).status_code, 302)

    def test_workspace_entry_and_profile(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertRegex(home.data, rb'<a[^>]*aria-label="Files"[^>]*aria-current="page">')
        self.assertIn("HR2026年刊项目规划.md".encode(), home.data)
        self.assertIn("源码".encode(), self.client.get("/files/intro?view=source").data)
        with patch("app.PUBLIC_ROSTER", False):
            cards = self.client.get("/people")
            self.assertIn("成员 002".encode(), cards.data)
            self.assertNotIn("示例组长".encode(), cards.data)
        self.login("lead")
        avatar = io.BytesIO()
        Image.new("RGB", (32, 48), "green").save(avatar, format="PNG")
        avatar.seek(0)
        self.assertEqual(self.post("/people/profile", {"bio": "负责头两章", "avatar": (avatar, "avatar.png")}).status_code, 302)
        with patch("app.PUBLIC_ROSTER", True):
            guest = app.test_client()
            self.assertIn("示例组长".encode(), guest.get("/people").data)
            self.assertIn("负责头两章".encode(), guest.get("/people/2").data)
            with guest.get("/people/2/avatar") as photo:
                self.assertEqual(photo.content_type, "image/jpeg")
                self.assertTrue(photo.data.startswith(b"\xff\xd8"))

    def test_file_group_scope_preview_and_versions(self):
        self.login("lead")
        self.assertEqual(self.upload("第1版.md", b"# Chapter draft\n\n<b>Plain text</b>").status_code, 302)
        preview = self.client.get("/files/1/preview")
        self.assertIn(b"<h1>Chapter draft</h1>", preview.data)
        self.assertIn(b"&lt;b&gt;Plain text", preview.data)
        self.assertEqual(self.upload("第2版.md", b"# Updated draft", previous=1).status_code, 302)
        self.assertEqual(self.upload("重复版.md", b"# Duplicate", previous=1).status_code, 409)
        with app.app_context(), db():
            db().execute("UPDATE files SET public=0 WHERE id IN (1, 2)")
        guest = app.test_client()
        for client in (guest, self.client):
            for old, latest in (
                ("/files?group=1&file=1", "/files?group=1&file=2"),
                ("/files/1/preview?view=source", "/files/2/preview?view=source"),
                ("/files/1/download", "/files/2/download"),
            ):
                with self.subTest(old=old):
                    with client.get(old) as response:
                        self.assertEqual((response.status_code, response.location), (302, latest))
                        self.assertNotIn(b"Chapter draft", response.data)
                    with client.get(old, follow_redirects=True) as response:
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(b"Updated draft", response.data)
                        self.assertNotIn("第1版.md".encode(), response.data)
            updated = client.get("/files?group=1&file=2")
            self.assertNotIn("历史版本".encode(), updated.data)
        self.assertEqual(guest.get("/files/2/inline").status_code, 404)
        self.login("outsider")
        self.assertEqual(self.upload("另组不能写.md", b"# No").status_code, 403)
        self.assertEqual(self.post("/files/1/manage", {"action": "withdraw"}).status_code, 403)
        self.login("lead")
        self.assertEqual(self.post("/files/1/manage", {"action": "publish"}).status_code, 400)
        self.assertEqual(self.post("/files/1/manage", {"action": "withdraw"}).status_code, 302)
        self.assertEqual(guest.get("/files/1/download").status_code, 404)
        self.assertNotIn("第1版.md".encode(), guest.get("/files?group=1&file=2").data)

    def test_existing_group_images_open_in_public_markdown_preview(self):
        self.login("lead")
        image = io.BytesIO()
        Image.new("RGB", (20, 16), "blue").save(image, format="PNG")
        original = image.getvalue()
        self.assertEqual(self.upload("分工图.png", original).status_code, 302)
        self.assertEqual(
            self.upload("说明.md", "![本地图片](./分工图.png)\n\n![直接地址](/files/1/inline)".encode()).status_code,
            302,
        )
        with app.app_context(), db():
            db().execute("UPDATE files SET public=0")
        guest = app.test_client()
        page = guest.get("/files/2/preview")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data.count(b'src="/files/1/inline"'), 2)
        with guest.get("/files/1/inline") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(response.data, original)
        updated_image = io.BytesIO()
        Image.new("RGB", (20, 16), "green").save(updated_image, format="PNG")
        self.assertEqual(self.upload("新版分工图.png", updated_image.getvalue(), previous=1).status_code, 302)
        for client in (guest, self.client):
            page = client.get("/files/2/preview")
            self.assertEqual(page.data.count(b'src="/files/3/inline"'), 2)
            self.assertNotIn(b'src="/files/1/inline"', page.data)
            with client.get("/files/1/inline") as response:
                self.assertEqual((response.status_code, response.location), (302, "/files/3/inline"))
            with client.get("/files/1/inline", follow_redirects=True) as response:
                self.assertEqual((response.status_code, response.mimetype, response.data),
                                 (200, "image/png", updated_image.getvalue()))
        self.assertEqual(self.post("/files/1/manage", {"action": "withdraw"}).status_code, 302)
        self.assertEqual(guest.get("/files/1/inline").status_code, 404)
        listing = guest.get("/files?group=1").data
        self.assertNotIn("<strong>分工图.png</strong>".encode(), listing)
        self.assertIn("<strong>新版分工图.png</strong>".encode(), listing)

    def test_excel_tabs_and_inert_code(self):
        self.login("lead")
        workbook = Workbook()
        workbook.active.title = "汇总"
        workbook.active.append(["header", 42])
        workbook.create_sheet("成本").append(["second sheet", 11])
        stream = io.BytesIO()
        workbook.save(stream)
        self.assertEqual(self.upload("工作表.xlsx", stream.getvalue()).status_code, 302)
        self.assertIn(b"second sheet", self.client.get("/files/1/preview?sheet=1").data)
        self.assertEqual(self.upload("示例.html", b"<h1>raw code</h1>").status_code, 302)
        self.assertIn(b"&lt;h1&gt;raw code", self.client.get("/files/2/preview").data)
        self.assertEqual(self.client.get("/files/2/inline").status_code, 404)
        self.assertEqual(self.upload("unsupported.exe", b"test").status_code, 400)
        app.config["MAX_CONTENT_LENGTH"] = 1024
        self.assertEqual(self.upload("large.md", b"x" * 2000).status_code, 413)

    def test_task_state_review_dependency_and_contribution(self):
        with app.app_context(), db():
            db().execute("""INSERT INTO tasks(id,title,week,group_id,parent_id,owner_id,reviewer_id,due,deliverable)
                          VALUES(2,'章节提纲',13,1,1,5,2,'2026-11-22','选题单')""")
        self.login("liaison")
        self.assertEqual(self.post("/task/2/progress", {"status": "waiting", "risk": "green"}).status_code, 400)
        self.assertEqual(self.post("/task/2/progress", {"status": "waiting", "risk": "green", "waiting_for": "2",
                                                      "blocker": "等组长审核", "reply_due": "2026-11-20"}).status_code, 302)
        self.assertEqual(self.post("/task/2/progress", {"status": "done", "risk": "green"}).status_code, 403)
        self.login("editor")
        self.assertEqual(self.post("/task/1/progress", {"status": "doing", "risk": "green"}).status_code, 302)
        self.assertEqual(self.post("/task/1/progress", {"status": "done", "risk": "green"}).status_code, 409)
        self.login("lead")
        self.assertEqual(self.post("/task/2/progress", {"status": "done", "risk": "green"}).status_code, 302)
        self.login("editor")
        self.assertEqual(self.post("/task/1/progress", {"status": "done", "risk": "green"}).status_code, 302)
        self.assertIn("章节提纲".encode(), self.client.get("/people/5").data)

    def test_issues_and_wiki_require_the_right_member_actions(self):
        self.login("lead")
        result = self.post("/issues", {"title": "术语口径", "body": "统一岗位说明", "mention": "5", "task": "1"})
        self.assertEqual(result.status_code, 302)
        self.login("outsider")
        self.assertEqual(self.post("/issues/1", {"body": "结论", "resolve": "1"}).status_code, 403)
        self.assertEqual(self.post("/issues/1", {"body": "试读意见"}).status_code, 302)
        self.assertEqual(self.post("/wiki", {"title": "参考资料", "url": "https://example.org/material", "category": "参考文献"}).status_code, 302)
        self.assertEqual(self.post("/wiki", {"title": "非网页协议", "url": "mailto:example@example.org", "category": "参考文献"}).status_code, 400)

    def test_rate_limit_and_account_reset(self):
        self.login("editor")
        other = app.test_client()
        self.login("outsider", other)
        self.assertEqual(self.post("/people/4/reset", {"password": self.current}).status_code, 302)
        self.assertEqual(other.get("/password").status_code, 302)
        for _ in range(20):
            self.assertEqual(self.post("/login", {"username": "unknown", "password": "wrong"}, other).status_code, 200)
        self.assertEqual(self.post("/login", {"username": "unknown", "password": "wrong"}, other).status_code, 429)

    def test_sqlite_consistency_after_reopen(self):
        self.login("lead")
        self.upload("过程记录.md", b"entry")
        with app.app_context():
            init_db()
        with app.app_context():
            self.assertEqual(db().execute("PRAGMA integrity_check").fetchone()[0], "ok")
            row = db().execute("SELECT blob FROM files WHERE name='过程记录.md'").fetchone()
            self.assertEqual((DATA / "files" / row[0]).read_bytes(), b"entry")

    def test_module_tab_icons_cover_guest_pages_and_standalone_preview(self):
        self.assertEqual(self.login("lead").status_code, 302)
        upload = self.upload("icon-preview.txt", b"example")
        self.assertEqual(upload.status_code, 302)
        file_id = int(upload.location.rsplit("file=", 1)[-1])
        issue = self.post("/issues", {"title": "图标验收", "body": "检查议题详情"})
        self.assertEqual(issue.status_code, 302)
        visitor = app.test_client()
        routes = (
            ("/", "files"), ("/files", "files"), ("/files/intro?view=source", "files"),
            (f"/files/{file_id}/preview", "files"),
            ("/task?week=13", "task"), ("/issues", "issues"), (issue.location, "issues"),
            ("/wiki?tag=参考", "wiki"), ("/people", "people"), ("/people/2", "people"), ("/activity", "people"),
        )
        for url, icon in routes:
            with self.subTest(url=url):
                page = visitor.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.data.count(b'<link rel="icon"'), 1)
                self.assertIn(f'href="/static/favicons/{icon}.png?v=1"'.encode(), page.data)

        for icon in ("files", "task", "issues", "wiki", "people"):
            with self.subTest(icon=icon), visitor.get(f"/static/favicons/{icon}.png?v=1") as asset:
                self.assertEqual(asset.status_code, 200)
                self.assertEqual(asset.mimetype, "image/png")
                with Image.open(io.BytesIO(asset.data)) as image:
                    self.assertEqual((image.format, image.size), ("PNG", (64, 64)))


if __name__ == "__main__":
    unittest.main()

