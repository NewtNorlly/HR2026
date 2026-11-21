"""Exercise the three project roles against an isolated copy of the application."""

import io
import os
import sqlite3
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from openpyxl import Workbook
from PIL import Image

import test_app
from app import DATA, app, db


class RoleJourneyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.SiteTests()
        self.fixture.setUp()
        self.editor = app.test_client()
        self.manager = app.test_client()
        self.member = app.test_client()
        self.guest = app.test_client()

        for role_client, name, user_id, landing in (
            (self.editor, "示例主编", 1, "/"),
            (self.manager, "示例HR", 6, "/"),
            (self.member, "示例组员", 3, "/password"),
        ):
            with self.subTest(role=name):
                response = self.fixture.login(name, role_client)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, landing)
                with role_client.session_transaction() as session:
                    self.assertEqual(session["uid"], user_id)

    def post(self, client, path, expected=302, **data):
        response = self.fixture.post(path, data, client)
        self.assertEqual(response.status_code, expected, path)
        return response

    def member_change_password(self, temporary=None, updated=None):
        return self.post(
            self.member, "/password",
            current=temporary or self.fixture.temporary,
            new=updated or self.fixture.current,
            confirm=updated or self.fixture.current,
        )

    def assert_page_contains(self, client, path, words):
        response = client.get(path)
        self.assertEqual(response.status_code, 200, path)
        self.assertIn(words, response.get_data(as_text=True), path)

    def upload(self, client, name, payload, *, group=0, public=False, previous=None, expected=302):
        fields = {"group": str(group), "document": (io.BytesIO(payload), name)}
        if public:
            fields["public"] = "1"
        if previous is not None:
            fields["previous"] = str(previous)
        response = self.post(client, "/files/upload", expected, **fields)
        if expected == 302:
            return int(parse_qs(urlsplit(response.location).query)["file"][0])

    def task_form(self, client, title, *, owner, reviewer, expected=302, **extra):
        return self.post(
            client, "/task/save", expected,
            title=title, week="13", due="2026-11-22", deliverable="核对成果后确认",
            owner=str(owner), reviewer=str(reviewer), **extra,
        )

    def workbook_bytes(self):
        workbook = Workbook()
        workbook.active.title = "核对表"
        workbook.active.append(["项目", "成员数"])
        workbook.active.append(["任务演练", 3])
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        return buffer.getvalue()

    def test_accounts_password_change_roles_and_logout(self):
        for client in (self.editor, self.manager):
            for path, expected in (
                ("/", "HR2026年刊项目规划.md"), ("/task", "Officer"), ("/issues", "Issues"),
                ("/wiki", "Wiki"), ("/people", "成员名片"), ("/activity", "变更记录"),
            ):
                self.assert_page_contains(client, path, expected)

        blocked = self.post(
            self.member, "/issues", title="改密前不应创建", body="测试",
        )
        self.assertEqual(blocked.location, "/password")
        self.assertEqual(self.member.get("/people").location, "/password")
        self.member_change_password()
        self.assert_page_contains(self.member, "/", "HR2026年刊项目规划.md")

        absent_token = self.member.post("/issues", data={"title": "无令牌", "body": "测试"})
        self.assertEqual(absent_token.status_code, 400)
        other_token = self.fixture.token(self.editor)
        forged_session = self.member.post("/issues", data={"csrf": other_token, "title": "其他会话令牌", "body": "测试"})
        self.assertEqual(forged_session.status_code, 400)
        self.assertNotIn("无令牌", self.guest.get("/issues").get_data(as_text=True))

        self.post(self.member, "/people/1/reset", 403, password="Another-passphrase-2026")
        self.post(self.manager, "/people/1/reset", 403, password="Another-passphrase-2026")
        self.post(
            self.manager, "/people/create", 403,
            username="another-hr", name="另一位协调员", password="Another-passphrase-2026", role="hr",
        )
        response = self.post(
            self.manager, "/people/create",
            username="created-member", name="协调员建立的成员", password="Another-passphrase-2026",
            role="member", group="2",
        )
        self.assert_page_contains(self.editor, response.location, "协调员建立的成员")
        reissued = "Reissued-passphrase-2026"
        self.post(self.manager, "/people/3/reset", password=reissued)
        self.assertEqual(self.member.get("/password").location, "/login")
        guest_activity = self.guest.get("/activity")
        self.assertEqual(guest_activity.status_code, 200)
        self.assertNotIn("重置成员密码", guest_activity.get_data(as_text=True))
        self.assertEqual(self.fixture.login("示例组员", self.member, self.fixture.current).status_code, 200)
        self.assertEqual(self.fixture.login("示例组员", self.member, reissued).location, "/password")
        self.member_change_password(temporary=reissued, updated="Replaced-passphrase-2026")
        self.post(self.member, "/logout")
        self.assertEqual(self.member.get("/password").location, "/login")
        self.assertEqual(self.editor.get("/activity").status_code, 200)
        self.assertEqual(self.manager.get("/activity").status_code, 200)

    def test_editor_assigns_member_manager_reviews_and_reopens(self):
        self.member_change_password()
        created = self.task_form(
            self.editor, "项目统筹：核对课程清单",
            owner=3, reviewer=6, parent="1",
        )
        for client in (self.editor, self.manager, self.member, self.guest):
            focused = client.get(created.location)
            self.assertEqual(focused.status_code, 200)
            self.assertIn('id="task-2" open', focused.get_data(as_text=True))
        self.task_form(self.manager, "跨组协调：收集参考资料", owner=6, reviewer=1)
        self.task_form(self.member, "普通成员不应创建统筹事项", owner=3, reviewer=6, expected=403)

        self.post(self.member, "/task/3/progress", 403, status="doing", risk="green")
        self.post(self.member, "/task/2/progress", status="doing", risk="green")
        self.post(self.member, "/task/2/progress", 403, status="done", risk="green")
        self.post(self.editor, "/task/1/progress", status="doing", risk="green")
        self.post(self.editor, "/task/1/progress", 409, status="done", risk="green")

        submission = self.upload(self.member, "课程清单.md", b"# Checklist", group=1)
        with app.app_context(), db():
            db().execute("UPDATE files SET public=0 WHERE id=?", (submission,))
        self.post(
            self.member, "/task/2/progress",
            status="waiting", risk="yellow", waiting_for="6", blocker="等待协调员验收",
            reply_due="2026-11-21", file_id=str(submission),
        )
        self.assert_page_contains(self.manager, "/task", "等待协调员验收")
        self.assert_page_contains(self.guest, "/task", "课程清单.md")
        accepted = self.post(self.manager, "/task/2/progress", status="done", risk="green", file_id=str(submission))
        self.assert_page_contains(self.manager, accepted.location, "✓ 已完成")
        self.assert_page_contains(self.member, "/people/3", "项目统筹：核对课程清单")
        self.assert_page_contains(self.guest, "/people/3", "已验收交付")
        self.assert_page_contains(self.guest, "/people/3", "课程清单.md")
        latest_submission = self.upload(self.member, "最新课程清单.md", b"# Updated checklist",
                                        group=1, previous=submission)
        current_link = f'href="/files?group=1&amp;file={latest_submission}"'
        for client in (self.guest, self.editor, self.member):
            for path in ("/task", "/people/3"):
                with self.subTest(path=path):
                    html = client.get(path).get_data(as_text=True)
                    self.assertIn(current_link, html)
                    self.assertNotIn(f'href="/files/{submission}/preview"', html)
                    self.assertNotIn(f'href="/files?group=1&amp;file={submission}"', html)
        with app.app_context():
            self.assertEqual(db().execute("SELECT file_id FROM tasks WHERE id=2").fetchone()[0], submission)
            self.assertEqual(db().execute("SELECT file_id FROM contributions WHERE task_id=2").fetchone()[0],
                             submission)
        self.post(self.editor, "/task/1/progress", status="done", risk="green")
        self.post(self.manager, "/task/2/progress", 409, status="doing", risk="green")
        self.post(self.editor, "/task/1/progress", status="doing", risk="green")
        self.post(self.manager, "/task/2/progress", status="doing", risk="green")
        self.assertNotIn(current_link, self.guest.get("/people/3").get_data(as_text=True))
        with app.app_context():
            row = db().execute("SELECT status FROM contributions WHERE task_id=2").fetchone()
            self.assertEqual(row["status"], "reopened")

    def test_files_visibility_versions_preview_and_access_boundaries(self):
        self.member_change_password()
        note = self.upload(self.editor, "统筹说明.md", b"# Officer draft")
        ledger = self.upload(self.manager, "协调表.xlsx", self.workbook_bytes(), public=True)
        own_notes = self.upload(self.member, "小组记录.md", b"# Group 1 draft", group=1)
        for group in (0, 2):
            self.upload(self.member, "无权上传.md", b"Denied", group=group, expected=403)
        self.upload(self.manager, "负责人不可替他人写版本.md", b"Denied", previous=note, expected=403)
        self.post(self.member, f"/files/{note}/manage", 403, action="withdraw")
        guest_write = self.post(self.guest, "/files/upload", group="1", document=(io.BytesIO(b"Do not save"), "guest.md"))
        guest_withdraw = self.post(self.guest, f"/files/{note}/manage", action="withdraw")
        self.assertEqual(urlsplit(guest_write.location).path, "/login")
        self.assertEqual(urlsplit(guest_withdraw.location).path, "/login")
        self.assert_page_contains(self.guest, f"/files/{note}/preview", "Officer draft")
        self.assert_page_contains(self.editor, f"/files/{own_notes}/preview", "Group 1 draft")
        self.assert_page_contains(self.guest, f"/files/{own_notes}/preview", "Group 1 draft")
        self.assert_page_contains(self.guest, f"/files/{ledger}/preview", "任务演练")

        note_update = self.upload(self.editor, "统筹说明.md", b"# Officer second revision", previous=note)
        latest = f"/files?group=0&file={note_update}"
        self.assert_page_contains(self.guest, latest, "Officer second revision")
        for client in (self.guest, self.manager):
            older = client.get(f"/files?group=0&file={note}")
            self.assertEqual((older.status_code, older.location), (302, latest))
            self.assertNotIn("历史版本", client.get(latest).get_data(as_text=True))
        controls = self.manager.get(f"/files?group=0&file={note_update}").get_data(as_text=True)
        self.assertNotIn("允许访客预览和下载此版本", controls)
        self.assertNotIn("公开此版本", controls)
        self.assert_page_contains(self.guest, f"/files/{note_update}/preview", "Officer second revision")
        with self.guest.get(f"/files/{note}/download", follow_redirects=True) as download:
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.data, b"# Officer second revision")

        self.post(self.editor, f"/files/{note_update}/manage", 400, action="unpublish")
        self.assert_page_contains(self.guest, f"/files/{note_update}/preview", "Officer second revision")
        self.post(self.editor, f"/files/{ledger}/manage", action="withdraw")
        self.assertEqual(self.manager.get(f"/files/{ledger}/preview").status_code, 404)
        self.assertEqual(self.guest.get(f"/files/{ledger}/download").status_code, 404)
        self.assertNotIn("协调表.xlsx", self.guest.get("/files?group=0").get_data(as_text=True))

    def test_issues_wiki_profiles_and_cross_role_review(self):
        self.member_change_password()
        issue = self.post(
            self.editor, "/issues", title="课程材料协调",
            body="请核对资料内容", mention="3", assignee="6", task="1",
        )
        self.assert_page_contains(self.member, "/issues?scope=mentioned", "课程材料协调")
        self.post(self.member, "/issues/1/assign", 403, assignee="3")
        self.post(self.member, "/issues/1", 403, body="不能代替负责人结案", resolve="1")
        self.post(self.member, "/issues/1", body="普通成员已核对清单", mention="6")
        self.post(self.manager, "/issues/1/assign", assignee="3")
        self.post(self.manager, "/issues/1/comments/1/react", reaction="+1")
        self.post(self.member, "/issues/1/react", reaction="hooray")
        self.post(self.editor, "/issues/1", body="核对通过", resolve="1")
        for phrase in ("普通成员已核对清单", "核对通过", "👍 1", "🎉 1"):
            self.assert_page_contains(self.guest, issue.location, phrase)
        self.assert_page_contains(self.manager, "/issues?scope=assigned", "课程材料协调")

        card = dict(title="公共课程目录", url="https://example.org/courses", category="参考", stars="4")
        self.post(self.member, "/wiki", **card)
        self.post(self.manager, "/wiki/1/update", 403, **card)
        self.post(self.member, "/wiki/1/update", **{**card, "stars": "5"})
        self.assert_page_contains(self.guest, "/wiki?tag=参考", "★★★★★")

        self.post(self.member, "/people/profile", bio="负责课表")
        self.assert_page_contains(self.editor, "/people/3", "负责课表")
        self.assert_page_contains(self.guest, "/activity", "课程材料协调")

    def test_people_member_detail_and_list_use_the_same_activity_counts(self):
        self.member_change_password()

        def assert_counts(uploads, discussions):
            for client in (self.guest, self.member):
                detail = client.get("/people/3")
                self.assertEqual(detail.status_code, 200)
                listing = client.get("/people").get_data(as_text=True)
                member_card = listing.split('class="person-card" href="/people/3"', 1)[1].split("</a>", 1)[0]
                for markup in (member_card, detail.get_data(as_text=True)):
                    self.assertIn(f"<strong>{uploads}</strong> 上传", markup)
                    self.assertIn(f"<strong>{discussions}</strong> 议题与评论", markup)

        assert_counts(0, 0)
        first = self.upload(self.member, "采编索引.txt", b"index", group=1)
        withdrawn = self.upload(self.member, "待整理.txt", b"draft", group=1)
        self.post(self.editor, f"/files/{withdrawn}/manage", action="withdraw")
        self.upload(self.editor, "他人资料.txt", b"other")
        self.post(self.member, "/issues", title="资料索引", body="先试一章")
        self.post(self.member, "/issues/1", body="留下资料出处")
        self.post(self.editor, "/issues/1", body="他人的回复")
        self.post(self.member, "/issues/1/react", reaction="+1")
        assert_counts(1, 2)

        self.upload(self.member, "采编索引.txt", b"updated", group=1, previous=first)
        assert_counts(2, 2)
        self.assertEqual(self.guest.get("/people/99999").status_code, 404)

    def test_backup_restores_group_files_avatars_and_cross_module_state(self):
        self.member_change_password()
        payload = b"# A durable project artifact\n"
        document = self.upload(self.member, "备份演练.md", payload, group=1, public=True)
        portrait = io.BytesIO()
        Image.new("RGB", (24, 28), "navy").save(portrait, format="PNG")
        portrait.seek(0)
        self.post(self.member, "/people/profile", bio="验收材料",
                  avatar=(portrait, "avatar.png"))
        self.post(self.editor, "/issues", title="备份核对", body="恢复后仍可追溯", assignee="6")
        self.task_form(self.manager, "检查备份", owner=6, reviewer=1)

        with TemporaryDirectory(prefix="hr2026-backup-proof-") as path:
            scratch = Path(path)
            backup_dir = scratch / "backup"
            restored = scratch / "restored"
            restored.mkdir()
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("backup.py"))],
                env={**os.environ, "HR2026_DATA": str(DATA), "HR2026_BACKUP": str(backup_dir)},
                capture_output=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            (archive_path,) = backup_dir.glob("*.tar.gz")
            with tarfile.open(archive_path) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    destination = (restored / member.name).resolve()
                    self.assertIn(restored.resolve(), destination.parents)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        destination.write_bytes(source.read())
            restored_db = sqlite3.connect(restored / "project.sqlite3")
            try:
                self.assertEqual(restored_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(restored_db.execute("PRAGMA foreign_key_check").fetchall(), [])
                (blob, public) = restored_db.execute("SELECT blob,public FROM files WHERE id=?", (document,)).fetchone()
                self.assertEqual(public, 1)
                self.assertEqual((restored / "files" / blob).read_bytes(), payload)
                (avatar, bio) = restored_db.execute("SELECT avatar,bio FROM profiles WHERE member_id=3").fetchone()
                self.assertEqual(bio, "验收材料")
                self.assertTrue((restored / "avatars" / avatar).read_bytes().startswith(b"\xff\xd8"))
                self.assertEqual(restored_db.execute("SELECT title FROM issues").fetchone()[0], "备份核对")
                self.assertEqual(restored_db.execute("SELECT owner_id FROM tasks WHERE title='检查备份'").fetchone()[0], 6)
            finally:
                restored_db.close()


if __name__ == "__main__":
    unittest.main()
