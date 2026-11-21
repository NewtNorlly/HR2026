"""Exercise public folder organization and its role boundaries through Files."""

import io
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

import test_app
from app import app, db, init_db


def selection(response, key):
    return int(parse_qs(urlsplit(response.location).query)[key][0])


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.SiteTests()
        self.fixture.setUp()
        self.addCleanup(self.remove_test_folders)
        self.guest = app.test_client()

    @staticmethod
    def remove_test_folders():
        # Clear the synthetic rows before another module reuses test_app's sandbox.
        with app.app_context(), db():
            db().execute("UPDATE files SET folder_id=NULL")
            db().execute("DELETE FROM file_folders")

    def sign_in(self, username):
        client = app.test_client()
        self.assertEqual(self.fixture.login(username, client).status_code, 302)
        if username == "member":
            self.post(client, "/password", current=self.fixture.temporary,
                      new=self.fixture.current, confirm=self.fixture.current)
        return client

    def post(self, client, path, expected=302, **fields):
        response = self.fixture.post(path, fields, client)
        self.assertEqual(response.status_code, expected, path)
        return response

    def folder(self, client, name, *, group=1, parent=None, expected=302):
        data = {"group": str(group), "name": name}
        if parent is not None:
            data["parent"] = str(parent)
        response = self.post(client, "/files/folders", expected, **data)
        return selection(response, "folder") if expected == 302 else response

    def upload(self, client, name, payload, *, group=1, folder=None, previous=None, expected=302):
        data = {"group": str(group), "document": (io.BytesIO(payload), name)}
        if folder is not None:
            data["folder"] = str(folder)
        if previous is not None:
            data["previous"] = str(previous)
        response = self.post(client, "/files/upload", expected, **data)
        return selection(response, "file") if expected == 302 else response

    def read(self, path):
        response = self.guest.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    @staticmethod
    def png(color):
        buffer = io.BytesIO()
        with Image.new("RGB", (24, 16), color) as image:
            image.save(buffer, "PNG")
        return buffer.getvalue()

    def test_member_upload_and_public_read_inside_nested_folders(self):
        lead = self.sign_in("lead")
        member = self.sign_in("member")
        source = self.folder(lead, "采访整理")
        notes = self.folder(lead, "现场图与记录", parent=source)
        reference = self.folder(lead, "参考样张", parent=source)

        original = self.png("blue")
        photo = self.upload(member, "同名示意.png", original, folder=notes)
        comment = "# 走访摘要\n\n![现场图](./同名示意.png)".encode()
        markdown = self.upload(member, "走访摘要.md", comment, folder=notes)
        other_photo = self.upload(member, "同名示意.png", self.png("red"), folder=reference)

        root = self.read("/files?group=1")
        self.assertIn("采访整理", root)
        opened = self.read(f"/files?group=1&folder={notes}")
        self.assertIn("现场图与记录", opened)
        self.assertIn("走访摘要.md", opened)
        self.assertNotIn('action="/files/folders"', opened)
        self.assertNotIn('action="/files/upload"', opened)
        page = self.read(f"/files/{markdown}/preview")
        self.assertIn("<h1>走访摘要</h1>", page)
        self.assertIn(f'src="/files/{photo}/inline"', page)
        self.assertNotIn(f'src="/files/{other_photo}/inline"', page)
        with self.guest.get(f"/files/{photo}/inline") as inline:
            self.assertEqual((inline.status_code, inline.mimetype, inline.data), (200, "image/png", original))
        with self.guest.get(f"/files/{markdown}/download") as download:
            self.assertEqual((download.status_code, download.data), (200, comment))
        self.assertEqual(self.guest.get(f"/files?group=2&folder={notes}").status_code, 404)

    def test_folder_management_respects_roles_roots_and_sibling_names(self):
        editor, manager, lead, member = (self.sign_in(role) for role in ("editor", "hr", "lead", "member"))
        main = self.folder(manager, "全刊交付", group=0)
        group_one = self.folder(lead, "第一组过程资料")
        group_two = self.folder(editor, "第二组过程资料", group=2)
        guest = self.post(self.guest, "/files/folders", group="1", name="访客不可创建")
        self.assertEqual(urlsplit(guest.location).path, "/login")
        self.folder(member, "普通成员不可创建", expected=403)
        self.folder(lead, "组长不可管理全刊目录", group=0, expected=403)
        self.folder(lead, "组长不可管理别组目录", group=2, expected=403)
        self.folder(manager, "协调员不可管理小组目录", expected=403)
        self.folder(editor, "跨根目录不可用", group=1, parent=group_two, expected=404)
        self.upload(member, "跨根目录不可上传.txt", b"data", folder=main, expected=404)

        retained = self.folder(lead, "稿件", parent=group_one)
        self.folder(lead, "稿件", parent=group_one, expected=409)
        self.folder(editor, "稿件", group=2, parent=group_two)
        for invalid in ("", ".", "..", "上级/下级", "上级\\下级"):
            with self.subTest(name=invalid):
                self.folder(lead, invalid, parent=group_one, expected=400)

        self.post(manager, f"/files/folders/{retained}/rename", 403, name="无权改名")
        self.post(lead, f"/files/folders/{retained}/rename", name="在写稿件")
        self.assertIn("在写稿件", self.read(f"/files?group=1&folder={retained}"))
        deepest = retained
        for level in range(3, 7):
            deepest = self.folder(lead, f"第 {level} 层", parent=deepest)
        self.folder(lead, "第七层不可创建", parent=deepest, expected=400)

    def test_move_keeps_history_original_ids_and_future_upload_location(self):
        lead, editor, member = (self.sign_in(role) for role in ("lead", "editor", "member"))
        working = self.folder(lead, "进行中")
        destination = self.folder(lead, "组内归档", parent=working)
        another_root = self.folder(editor, "其他小组档案", group=2)
        first = self.upload(member, "研究稿一版.txt", b"version one", folder=working)
        second = self.upload(member, "研究稿二版.txt", b"version two", previous=first)

        guest = self.post(self.guest, f"/files/{second}/move", folder=str(destination))
        self.assertEqual(urlsplit(guest.location).path, "/login")
        self.post(member, f"/files/{second}/move", 403, folder=str(destination))
        self.post(lead, f"/files/{second}/move", 404, folder=str(another_root))
        self.post(lead, f"/files/{second}/move", folder=str(destination))
        third = self.upload(member, "研究稿三版.txt", b"version three", folder=working, previous=second)

        current = self.read(f"/files?group=1&file={third}")
        self.assertIn("组内归档", current)
        self.assertNotIn("研究稿一版.txt", current)
        self.assertNotIn("研究稿二版.txt", current)
        self.assertNotIn("历史版本", current)
        for client in (self.guest, lead):
            for file_id in (first, second):
                old = f"/files?group=1&folder={working}&file={file_id}"
                redirect = client.get(old)
                self.assertEqual(redirect.status_code, 302)
                self.assertEqual(selection(redirect, "file"), third)
                with client.get(old, follow_redirects=True) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b"version three", response.data)
                    self.assertNotIn(b"version one", response.data)
                    self.assertNotIn(b"version two", response.data)
                with client.get(f"/files/{file_id}/download", follow_redirects=True) as download:
                    self.assertEqual((download.status_code, download.data), (200, b"version three"))
        with app.app_context():
            chain = db().execute("SELECT id,previous_id,folder_id FROM files ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in chain], [
                (first, None, destination), (second, first, destination), (third, second, destination),
            ])

        self.post(lead, f"/files/{third}/move", folder="")
        self.assertIn("研究稿三版.txt", self.read("/files?group=1"))
        with app.app_context():
            folders = db().execute("SELECT DISTINCT folder_id FROM files").fetchall()
            self.assertEqual([tuple(row) for row in folders], [(None,)])

        self.post(lead, f"/files/{second}/manage", action="withdraw")
        for client in (self.guest, lead):
            first_preview = client.get(f"/files/{first}/preview")
            self.assertEqual((first_preview.status_code, first_preview.location),
                             (302, f"/files/{third}/preview"))
            self.assertEqual(client.get(f"/files/{second}/preview").status_code, 404)
        self.post(lead, f"/files/{third}/manage", action="withdraw")
        self.assertNotIn("研究稿一版.txt", self.read("/files?group=1"))
        for client in (self.guest, lead):
            self.assertEqual(client.get(f"/files/{first}/preview").status_code, 404)
            self.assertEqual(client.get(f"/files/{third}/download").status_code, 404)

    def test_original_database_without_folder_columns_migrates_in_place(self):
        with TemporaryDirectory(prefix="hr2026-legacy-files-") as directory:
            data = Path(directory)
            saved = data / "files"
            saved.mkdir()
            original = "# 导入前已存在\n\n原件保留。".encode()
            (saved / "original-draft").write_bytes(original)
            with closing(sqlite3.connect(data / "project.sqlite3")) as legacy, legacy:
                legacy.executescript("""
                    CREATE TABLE files(
                        id INTEGER PRIMARY KEY, previous_id INTEGER UNIQUE REFERENCES files(id),
                        name TEXT NOT NULL, blob TEXT NOT NULL UNIQUE, ext TEXT NOT NULL, size INTEGER NOT NULL,
                        group_id INTEGER REFERENCES groups(id), owner_id INTEGER NOT NULL REFERENCES users(id),
                        public INTEGER NOT NULL DEFAULT 0, withdrawn INTEGER NOT NULL DEFAULT 0,
                        preview_status TEXT NOT NULL DEFAULT 'ready', uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                legacy.execute("INSERT INTO files(id,name,blob,ext,size,group_id,owner_id) VALUES(42,?,?,?,?,1,1)",
                               ("已有稿件.md", "original-draft", ".md", len(original)))
            with patch("app.DATA", data), patch("app.FILES", saved):
                with app.app_context():
                    init_db()
                    with db():
                        db().execute("INSERT INTO users(id,username,name,role,password) VALUES(1,'example','示例主编','editor','unused')")
                    init_db()
                    self.assertEqual(db().execute("PRAGMA foreign_key_check").fetchall(), [])
                    record = db().execute("SELECT public,folder_id FROM files WHERE id=42").fetchone()
                    self.assertEqual(tuple(record), (0, None))
                listing = self.read("/files?group=1")
                self.assertIn("已有稿件.md", listing)
                self.assertIn("<h1>导入前已存在</h1>", self.read("/files/42/preview"))
                with self.guest.get("/files/42/download") as download:
                    self.assertEqual((download.status_code, download.data), (200, original))


if __name__ == "__main__":
    unittest.main()
