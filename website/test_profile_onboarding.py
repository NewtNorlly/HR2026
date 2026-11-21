"""Exercise the first sign-in and the resulting People card through public routes."""

import io
import re
import unittest
from unittest.mock import patch
from urllib.parse import urldefrag

from PIL import Image

import test_app
from app import app, db


DEFAULT_PORTRAIT = "/static/researcher-avatar.webp?v=4"


def portraits(response, css_class):
    return re.findall(
        rf'<img\b[^>]*\bclass="{css_class}(?:\s[^"]*)?"[^>]*\bsrc="([^"]+)"',
        response.get_data(as_text=True),
    )


class ProfileOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.SiteTests()
        self.fixture.setUp()
        self.client = self.fixture.client
        self.visitor = app.test_client()

    def test_required_change_offers_profile_setup_once(self):
        login = self.fixture.login("示例组员")
        self.assertEqual(login.location, "/password")
        for path in ("/", "/people/3", "/people/3?edit=1"):
            self.assertEqual(self.client.get(path).location, "/password")
        self.assertEqual(portraits(self.client.get("/password"), "account-portrait"), [DEFAULT_PORTRAIT])

        fields = {"current": self.fixture.temporary, "new": self.fixture.current, "confirm": self.fixture.current}
        changed = self.fixture.post("/password", fields)
        self.assertEqual(changed.status_code, 302)
        welcome = self.client.get(changed.location)
        self.assertEqual(welcome.status_code, 200)
        self.assertIn("个人感想", welcome.get_data(as_text=True))
        setup_link = "/people/3?edit=1#settings"
        self.assertIn(f'href="{setup_link}"', welcome.get_data(as_text=True))
        self.assertIn("HR2026年刊项目规划.md", welcome.get_data(as_text=True))

        profile = self.client.get(urldefrag(setup_link).url)
        self.assertEqual(profile.status_code, 200)
        self.assertRegex(profile.get_data(as_text=True), r'<details(?=[^>]*\bid="settings")(?=[^>]*\bopen\b)[^>]*>')
        self.assertIn('name="avatar"', profile.get_data(as_text=True))
        self.assertIn('name="bio"', profile.get_data(as_text=True))
        self.assertNotIn('name="nickname"', profile.get_data(as_text=True))
        self.assertNotIn(f'href="{setup_link}"', self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get("/task").status_code, 200)

        voluntary = self.fixture.post("/password", {
            "current": self.fixture.current,
            "new": "Voluntary-passphrase-2026",
            "confirm": "Voluntary-passphrase-2026",
        })
        self.assertEqual(voluntary.status_code, 302)
        self.assertNotIn(f'href="{setup_link}"', self.client.get(voluntary.location).get_data(as_text=True))

    def test_default_and_custom_portraits_and_saved_reflection(self):
        home = self.visitor.get("/")
        self.assertEqual(portraits(home, "account-portrait"), [DEFAULT_PORTRAIT])
        with self.visitor.get(DEFAULT_PORTRAIT) as image:
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.mimetype, "image/webp")
            self.assertEqual(Image.open(io.BytesIO(image.data)).format, "WEBP")

        with patch("app.PUBLIC_ROSTER", True):
            listing = self.visitor.get("/people")
            self.assertEqual(portraits(listing, "avatar"), [DEFAULT_PORTRAIT] * 6)
            self.assertEqual(portraits(self.visitor.get("/people/2"), "avatar"), [DEFAULT_PORTRAIT])

            self.assertEqual(self.fixture.login("lead").status_code, 302)
            avatar = io.BytesIO()
            Image.new("RGB", (48, 52), "navy").save(avatar, format="PNG")
            avatar.seek(0)
            reflection = "我在核对资料时学会了回查引用。"
            saved = self.fixture.post("/people/profile", {
                "bio": reflection, "avatar": (avatar, "portrait.png"),
            })
            self.assertEqual(saved.status_code, 302)

            custom_portrait = "/people/2/avatar"
            listing = self.visitor.get("/people")
            self.assertCountEqual(portraits(listing, "avatar"), [DEFAULT_PORTRAIT] * 5 + [custom_portrait])
            self.assertIn(reflection, listing.get_data(as_text=True))
            profile = self.visitor.get(saved.location)
            self.assertEqual(portraits(profile, "avatar"), [custom_portrait])
            self.assertIn("个人感想", profile.get_data(as_text=True))
            self.assertIn(reflection, profile.get_data(as_text=True))
            self.assertEqual(portraits(self.client.get("/"), "account-portrait"), [DEFAULT_PORTRAIT])
            with self.visitor.get(custom_portrait) as image:
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.mimetype, "image/jpeg")

        with patch("app.PUBLIC_ROSTER", False):
            listing = self.visitor.get("/people")
            self.assertNotIn(reflection, listing.get_data(as_text=True))
            self.assertEqual(portraits(listing, "avatar"), [DEFAULT_PORTRAIT] * 6)
            self.assertEqual(self.visitor.get(custom_portrait).status_code, 404)

    def test_legacy_manual_entries_and_nicknames_cannot_change_the_card(self):
        legacy_nickname = "旧昵称不展示"
        legacy_manual_record = "旧人工登记的协作记录"
        with app.app_context(), db():
            db().execute("UPDATE users SET nickname=? WHERE id=2", (legacy_nickname,))
            legacy = db().execute(
                "INSERT INTO contributions(member_id,kind,detail,evidence,status) VALUES(2,'support',?,'','approved')",
                (legacy_manual_record,),
            ).lastrowid
        self.assertEqual(self.fixture.login("lead").status_code, 302)
        edited = self.fixture.post("/people/profile", {"nickname": "企图改昵称", "bio": "保留新感想"})
        self.assertEqual(edited.status_code, 302)
        self.assertEqual(self.fixture.post("/people/nickname", {"nickname": "旧接口"}).status_code, 404)
        self.assertEqual(self.fixture.post("/people/2/contribution", {
            "kind": "support", "detail": "企图补报",
        }).status_code, 404)
        for client in (self.client, self.visitor):
            rendered = client.get("/people/2").get_data(as_text=True)
            self.assertNotIn(legacy_nickname, rendered)
            self.assertNotIn(legacy_manual_record, rendered)
            self.assertNotIn("企图改昵称", rendered)
            self.assertNotIn("提交复核", rendered)
        self.assertIn("保留新感想", self.client.get("/people/2").get_data(as_text=True))
        self.assertEqual(self.fixture.login("hr").status_code, 302)
        self.assertEqual(self.fixture.post(f"/people/contribution/{legacy}/review", {
            "status": "approved",
        }).status_code, 404)
        self.assertNotIn(legacy_manual_record, self.client.get("/people/2").get_data(as_text=True))
        with app.app_context():
            self.assertEqual(db().execute("SELECT nickname FROM users WHERE id=2").fetchone()[0], legacy_nickname)
            self.assertEqual(db().execute("SELECT COUNT(*) FROM contributions WHERE member_id=2").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
