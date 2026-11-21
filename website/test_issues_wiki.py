import io
import re
import unittest
from unittest.mock import patch
from urllib.parse import urldefrag

from PIL import Image

from test_app import app, db


DEFAULT_PORTRAIT = "/static/researcher-avatar.webp?v=4"


class IssuesWikiTests(unittest.TestCase):
    def setUp(self):
        with app.app_context(), db():
            for table in ("roster", "profiles", "comments", "issues", "contributions", "links", "audit", "tasks", "files", "users", "login_limits"):
                db().execute(f"DELETE FROM {table}")
            for number, name, role in [(1, "测试编辑", "editor"), (2, "测试组长", "lead"), (3, "测试成员", "member")]:
                db().execute(
                    "INSERT INTO users(id,username,name,role,group_id,must_change,password) VALUES(?,?,?,?,?,0,'unused')",
                    (number, f"test-{number}", name, role, None if role == "editor" else 1),
                )
        self.guest = self.client()
        self.editor = self.client(1)
        self.lead = self.client(2)
        self.member = self.client(3)

    def client(self, number=None):
        client = app.test_client()
        with client.session_transaction() as session:
            session["csrf"] = "test-csrf"
            if number is not None:
                session.update(uid=number, version=0)
        return client

    def post(self, client, path, fields):
        return client.post(path, data={"csrf": "test-csrf", **fields})

    def upload_portrait(self, client, color):
        upload = io.BytesIO()
        Image.new("RGB", (24, 24), color).save(upload, format="PNG")
        upload.seek(0)
        saved = self.post(client, "/people/profile", {"avatar": (upload, "avatar.png")})
        self.assertEqual(saved.status_code, 302)

    def test_public_issue_reads_and_personal_filters(self):
        root = self.post(self.lead, "/issues", {"title": "共享用语", "body": "统一部门名", "mention": "3", "assignee": "1"})
        self.assertEqual(root.status_code, 302)
        self.post(self.editor, "/issues", {"title": "另外一条议题", "body": "第二条公开内容"})
        self.assertIn("统一部门名".encode(), self.guest.get(root.location).data)
        self.assertNotIn(b'name="body"', self.guest.get("/issues").data)
        self.assertIn("共享用语".encode(), self.lead.get("/issues?scope=created").data)
        self.assertNotIn("另外一条议题".encode(), self.lead.get("/issues?scope=created").data)
        self.assertIn("共享用语".encode(), self.lead.get("/issues?scope=assigned").data)
        self.assertNotIn("共享用语".encode(), self.editor.get("/issues?scope=assigned").data)
        self.assertIn("共享用语".encode(), self.member.get("/issues?scope=mentioned").data)
        self.assertEqual(self.post(self.member, "/issues/1/assign", {"assignee": "3"}).status_code, 403)
        self.assertEqual(self.post(self.editor, "/issues/1/assign", {"assignee": "3"}).status_code, 302)
        self.assertIn("共享用语".encode(), self.editor.get("/issues?scope=assigned").data)
        self.assertEqual(self.post(self.guest, "/issues", {"title": "访客不能写", "body": "验证访问规则"}).status_code, 302)

    def test_replies_mentions_and_expression_toggles(self):
        self.post(self.lead, "/issues", {"title": "庆祝进展", "body": "已经开始"})
        self.post(self.lead, "/issues", {"title": "其他进展", "body": "已经开始"})
        self.assertEqual(self.post(self.editor, "/issues/1", {"body": "我补一条", "mention": "3"}).status_code, 302)
        self.assertIn("庆祝进展".encode(), self.member.get("/issues?scope=mentioned").data)
        self.assertEqual(self.post(self.member, "/issues/1/react", {"reaction": "hooray"}).status_code, 302)
        self.assertIn("🎉 1".encode(), self.guest.get("/issues?issue=1").data)
        self.assertEqual(self.post(self.member, "/issues/1/react", {"reaction": "hooray"}).status_code, 302)
        self.assertNotIn("🎉 1".encode(), self.guest.get("/issues?issue=1").data)
        self.assertEqual(self.post(self.member, "/issues/1/comments/1/react", {"reaction": "+1"}).status_code, 302)
        self.assertIn("👍 1".encode(), self.guest.get("/issues?issue=1").data)
        self.assertEqual(self.post(self.member, "/issues/2/comments/1/react", {"reaction": "+1"}).status_code, 404)
        self.assertEqual(self.post(self.member, "/issues/1/react", {"reaction": "unknown"}).status_code, 400)

    def test_only_authors_can_delete_issues_and_comments(self):
        thread = self.post(self.lead, "/issues", {"title": "作者保留议题", "body": "这条议题不应被他人删除"})
        self.post(self.lead, "/issues", {"title": "另一条议题", "body": "用于验证评论所属议题"})
        reply = self.post(self.member, "/issues/1", {"body": "这条评论不应被他人删除"})
        self.assertEqual((thread.status_code, reply.status_code), (302, 302))

        for path, author, other_member in (
            ("/issues/1/delete", self.lead, self.member),
            ("/issues/1/comments/1/delete", self.member, self.lead),
        ):
            with self.subTest(path=path):
                self.assertIn(f'action="{path}"', author.get(thread.location).get_data(as_text=True))
                for client in (self.editor, other_member):
                    self.assertNotIn(f'action="{path}"', client.get(thread.location).get_data(as_text=True))
                    self.assertEqual(self.post(client, path, {}).status_code, 403)
                self.assertEqual(self.post(self.guest, path, {}).location, "/login")
                self.assertEqual(author.get(path).status_code, 405)
                self.assertEqual(author.post(path).status_code, 400)

        wrong_thread = self.post(self.member, "/issues/2/comments/1/delete", {})
        self.assertEqual(wrong_thread.status_code, 404)
        contents = self.guest.get(thread.location).get_data(as_text=True)
        self.assertIn("这条议题不应被他人删除", contents)
        self.assertIn("这条评论不应被他人删除", contents)

    def test_deleting_content_preserves_other_authors_comments_and_people_counts(self):
        issue = self.post(self.lead, "/issues", {"title": "原议题标题", "body": "原议题正文", "assignee": "1"})
        self.post(self.editor, "/issues/1", {"body": "将由本人删除的评论"})
        self.post(self.member, "/issues/1", {"body": "继续保留的成员评论"})
        self.post(self.lead, "/issues/1", {"body": "原处理结论", "resolve": "1"})
        self.post(self.member, "/issues/1/react", {"reaction": "hooray"})
        self.post(self.editor, "/issues/1/comments/2/react", {"reaction": "+1"})

        def assert_discussion_count(member_id, expected):
            profile = self.guest.get(f"/people/{member_id}")
            self.assertEqual(profile.status_code, 200)
            self.assertRegex(profile.get_data(as_text=True), rf"<strong>{expected}</strong>\s*议题与评论")

        for member_id in (1, 2, 3):
            assert_discussion_count(member_id, 1)

        self.assertEqual(self.post(self.editor, "/issues/1/comments/1/delete", {}).status_code, 302)
        self.assertIn("1 条回复", self.guest.get("/issues").get_data(as_text=True))
        self.assertNotIn("将由本人删除的评论", self.guest.get(issue.location).get_data(as_text=True))
        response = self.post(self.member, "/issues/1/comments/1/react", {"reaction": "+1"})
        self.assertEqual(response.status_code, 404)
        assert_discussion_count(1, 0)

        self.assertEqual(self.post(self.lead, "/issues/1/delete", {}).status_code, 302)
        with patch("app.PUBLIC_ROSTER", True):
            for client in (self.guest, self.editor, self.member):
                with self.subTest(guest=client is self.guest, owner=client is self.member):
                    response = client.get(issue.location)
                    self.assertEqual(response.status_code, 200)
                    contents = response.get_data(as_text=True)
                    self.assertIn("议题已删除", contents)
                    self.assertIn("继续保留的成员评论", contents)
                    self.assertIn("测试成员", contents)
                    for obsolete in (
                        "原议题标题", "原议题正文", "原处理结论", "将由本人删除的评论", "🎉 1",
                        'action="/issues/1"', 'action="/issues/1/assign"', 'action="/issues/1/react"',
                        'action="/issues/1/comments/2/react"',
                    ):
                        self.assertNotIn(obsolete, contents)

        for path, fields in (
            ("/issues/1", {"body": "不能再回复"}),
            ("/issues/1", {"body": "不能再填写结论", "resolve": "1"}),
            ("/issues/1/assign", {"assignee": "3"}),
            ("/issues/1/react", {"reaction": "hooray"}),
            ("/issues/1/comments/2/react", {"reaction": "+1"}),
        ):
            with self.subTest(path=path, fields=fields):
                self.assertEqual(self.post(self.editor, path, fields).status_code, 404)

        assert_discussion_count(1, 0)
        assert_discussion_count(2, 0)
        assert_discussion_count(3, 1)

        with app.app_context():
            deleted_issue = db().execute("SELECT deleted,title,body,resolution FROM issues WHERE id=1").fetchone()
            deleted_comment = db().execute("SELECT deleted,body FROM comments WHERE id=1").fetchone()
            self.assertEqual(tuple(deleted_issue), (1, "", "", ""))
            self.assertEqual(tuple(deleted_comment), (1, ""))

        self.assertEqual(self.post(self.member, "/issues/1/comments/2/delete", {}).status_code, 302)
        self.assertNotIn('href="/issues?issue=1"', self.guest.get("/issues").get_data(as_text=True))
        empty_thread = self.guest.get(issue.location)
        self.assertEqual(empty_thread.status_code, 404)
        self.assertNotIn("继续保留的成员评论", empty_thread.get_data(as_text=True))
        assert_discussion_count(3, 0)

    def test_deleting_issue_removes_its_title_from_current_and_legacy_activity(self):
        title = "从项目记录移除的议题标题"
        other_title = "继续显示在项目记录的议题"
        self.assertEqual(self.post(self.lead, "/issues", {"title": title, "body": "原讨论"}).status_code, 302)
        self.assertEqual(self.post(self.editor, "/issues/1/assign", {"assignee": "3"}).status_code, 302)
        self.assertEqual(self.post(self.member, "/issues", {"title": other_title, "body": "另一条讨论"}).status_code, 302)
        with app.app_context(), db():
            db().execute("INSERT INTO audit(actor,event,detail) VALUES(?,?,?)", ("旧记录作者", "发起讨论", title))
            db().execute("INSERT INTO audit(actor,event,detail) VALUES(?,?,?)",
                         ("旧记录作者", "任务关联讨论已处理", f"已经改名的旧任务 · {title}"))

        self.assertIn(title, self.guest.get("/activity").get_data(as_text=True))
        self.assertEqual(self.post(self.lead, "/issues/1/delete", {}).status_code, 302)
        for client in (self.guest, self.editor):
            activity = client.get("/activity").get_data(as_text=True)
            self.assertNotIn(title, activity)
            self.assertIn(other_title, activity)
            self.assertIn("议题 #1", activity)

        with app.app_context():
            tracked = db().execute("SELECT detail FROM audit WHERE issue_id=1 ORDER BY id").fetchall()
            legacy = db().execute("SELECT detail FROM audit WHERE actor=? ORDER BY id", ("旧记录作者",)).fetchall()
            self.assertEqual([row["detail"] for row in tracked], ["议题 #1"] * 3)
            self.assertEqual([row["detail"] for row in legacy], ["议题 #1"] * 2)

    def test_issue_list_and_discussion_show_each_authors_portrait(self):
        for client, color in ((self.editor, "navy"), (self.lead, "green")):
            self.upload_portrait(client, color)

        issue = self.post(self.lead, "/issues", {"title": "核对访谈", "body": "讨论如何整理"})
        other = self.post(self.member, "/issues", {"title": "准备资料", "body": "后续查阅"})
        self.assertEqual(issue.status_code, 302)
        self.assertEqual(other.status_code, 302)
        for client, body, mention in (
            (self.editor, "请组员补充出处", "3"),
            (self.member, "我已补充出处", "1"),
            (self.lead, "收到，稍后核对", "3"),
        ):
            self.assertEqual(self.post(client, "/issues/1", {"body": body, "mention": mention}).status_code, 302)

        editor_portrait, lead_portrait = "/people/1/avatar", "/people/2/avatar"

        def issue_portraits(client, path):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            return re.findall(
                r'<img\b(?=[^>]*\bclass="[^"]*\bissue-avatar\b)[^>]*\bsrc="([^"]+)"',
                response.get_data(as_text=True),
            )

        with patch("app.PUBLIC_ROSTER", True):
            self.assertEqual(issue_portraits(self.guest, "/issues"), [DEFAULT_PORTRAIT, lead_portrait])
            self.assertEqual(issue_portraits(self.guest, issue.location), [lead_portrait, editor_portrait, DEFAULT_PORTRAIT, lead_portrait])

            # An older profile can reference an asset which is no longer on disk.
            with app.app_context(), db():
                db().execute("UPDATE profiles SET avatar='missing-issue-portrait.jpg' WHERE member_id=2")
            self.assertEqual(issue_portraits(self.guest, "/issues"), [DEFAULT_PORTRAIT] * 2)
            self.assertEqual(issue_portraits(self.guest, issue.location), [DEFAULT_PORTRAIT, editor_portrait, DEFAULT_PORTRAIT, DEFAULT_PORTRAIT])

        with patch("app.PUBLIC_ROSTER", False):
            self.assertEqual(issue_portraits(self.guest, issue.location), [DEFAULT_PORTRAIT] * 4)
            self.assertEqual(issue_portraits(self.member, issue.location), [DEFAULT_PORTRAIT, editor_portrait, DEFAULT_PORTRAIT, DEFAULT_PORTRAIT])

    def test_wiki_tag_stars_and_author_edit(self):
        fields = {"title": "阅读记录", "url": "https://example.org/resources", "category": "精读", "stars": "5"}
        result = self.post(self.member, "/wiki", fields)
        self.assertEqual(result.status_code, 302)
        self.assertIn("★★★★★".encode(), self.guest.get("/wiki?tag=精读").data)
        self.assertNotIn("阅读记录".encode(), self.guest.get("/wiki?tag=其他").data)
        self.assertEqual(self.post(self.lead, "/wiki/1/update", fields).status_code, 403)
        self.assertEqual(self.post(self.member, "/wiki/1/update", {**fields, "stars": "2"}).status_code, 302)
        self.assertIn("★★☆☆☆".encode(), self.guest.get("/wiki").data)
        for invalid in ("mailto:user@example.org", "http://localhost/", "http://127.0.0.1/", "https://example.org:99999/"):
            self.assertEqual(self.post(self.member, "/wiki", {**fields, "url": invalid}).status_code, 400)
        self.assertEqual(self.post(self.member, "/wiki", {**fields, "stars": "6"}).status_code, 400)
        self.assertEqual(self.post(self.guest, "/wiki", fields).status_code, 302)

    def test_wiki_cards_credit_the_original_publisher(self):
        self.upload_portrait(self.lead, "green")
        fields = {"title": "培训来源", "url": "https://example.org/training", "category": "参考", "stars": "3"}
        first = self.post(self.lead, "/wiki", fields)
        second = self.post(self.member, "/wiki", {**fields, "title": "绩效来源", "url": "https://example.org/review"})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        first_card = urldefrag(first.location).fragment
        second_card = urldefrag(second.location).fragment

        def assert_byline(response, card_id, member_id, name, portrait, title):
            self.assertEqual(response.status_code, 200)
            card = re.search(
                rf'<article\b[^>]*\bid="{re.escape(card_id)}"[^>]*>(.*?)</article>',
                response.get_data(as_text=True), re.DOTALL,
            )
            self.assertIsNotNone(card, card_id)
            self.assertIn(title, card[1])
            self.assertIn("发布者", card[1])
            self.assertRegex(card[1], rf'<img\b(?=[^>]*\bclass="[^"]*\bwiki-publisher-avatar\b)[^>]*\bsrc="{re.escape(portrait)}"')
            self.assertRegex(card[1], rf'<a\b(?=[^>]*\bclass="wiki-publisher-name")(?=[^>]*\bhref="/people/{member_id}")[^>]*>\s*{re.escape(name)}\s*</a>')

        with patch("app.PUBLIC_ROSTER", True):
            listing = self.guest.get("/wiki")
            assert_byline(listing, first_card, 2, "测试组长", "/people/2/avatar", "培训来源")
            assert_byline(listing, second_card, 3, "测试成员", DEFAULT_PORTRAIT, "绩效来源")
            with self.guest.get("/people/2/avatar") as avatar:
                self.assertEqual(avatar.status_code, 200)
                self.assertEqual(avatar.mimetype, "image/jpeg")

            updated = self.post(self.lead, "/wiki/1/update", {**fields, "title": "修订的培训来源", "stars": "5"})
            self.assertEqual(updated.status_code, 302)
            self.assertEqual(updated.location, first.location)
            assert_byline(self.guest.get("/wiki"), first_card, 2, "测试组长", "/people/2/avatar", "修订的培训来源")

        with patch("app.PUBLIC_ROSTER", False):
            listing = self.guest.get("/wiki")
            self.assertNotIn("测试组长", listing.get_data(as_text=True))
            assert_byline(listing, first_card, 2, "成员 002", DEFAULT_PORTRAIT, "修订的培训来源")
            assert_byline(listing, second_card, 3, "成员 003", DEFAULT_PORTRAIT, "绩效来源")
            assert_byline(self.editor.get("/wiki"), first_card, 2, "测试组长", "/people/2/avatar", "修订的培训来源")


if __name__ == "__main__":
    unittest.main()
