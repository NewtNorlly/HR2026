"""Release checks for uploaded project documents and their rendered previews."""

import io
import shutil
import unittest
from html import unescape
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

import test_app
from app import INTRO_FILE, app, db
from openpyxl import Workbook
from PIL import Image
from preview_worker import render


def word_document():
    """Build a small, valid Word package without test-only dependencies."""
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        '<w:p><w:r><w:t>HR2026 release review</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>项目进度与下一步</w:t></w:r></w:p>'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'
    )
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document.encode("utf-8"))
    return output.getvalue()


class ReleaseAssetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.SiteTests()
        self.fixture.setUp()
        self.assertEqual(self.fixture.login("lead").status_code, 302)
        self.client = self.fixture.client
        self.visitor = app.test_client()

    def upload(self, name, data, *, public=False):
        result = self.fixture.post(
            "/files/upload",
            {"group": "1", "public": "1" if public else "0", "document": (io.BytesIO(data), name)},
        )
        self.assertEqual(result.status_code, 302)
        return int(result.location.rsplit("file=", 1)[-1])

    @unittest.skipUnless(shutil.which("soffice"), "LibreOffice (soffice) is not installed")
    def test_word_upload_converts_and_serves_the_pdf_to_visitors(self):
        original = word_document()
        file_id = self.upload("发布验收.docx", original)
        preview_path = f"/files/{file_id}/preview"
        inline_path = f"/files/{file_id}/inline"
        self.assertIn("正在生成预览".encode(), self.client.get(preview_path).data)
        self.assertEqual(self.client.get(inline_path).status_code, 404)
        with app.app_context():
            record = db().execute("SELECT blob,ext,preview_status FROM files WHERE id=?", (file_id,)).fetchone()
        self.assertEqual(record["preview_status"], "pending")
        render(record["blob"], record["ext"])
        with app.app_context(), db():
            db().execute("UPDATE files SET preview_status='ready',public=0 WHERE id=?", (file_id,))
        self.assertIn(f'src="{inline_path}"'.encode(), self.visitor.get(preview_path).data)
        with self.visitor.get(inline_path) as pdf:
            self.assertEqual(pdf.status_code, 200)
            self.assertEqual(pdf.mimetype, "application/pdf")
            self.assertTrue(pdf.data.startswith(b"%PDF-"))
            self.assertTrue(pdf.data.rstrip().endswith(b"%%EOF"))
        with self.visitor.get(f"/files/{file_id}/download") as download:
            self.assertEqual(download.data, original)

    def test_xlsx_worksheets_paging_and_source_download(self):
        workbook = Workbook()
        workbook.active.title = "进度"
        workbook.active.append(["项目", "状态", "用时"])
        for number in range(1, 62):
            workbook.active.append([f"事项 {number}", "进行中", number])
        workbook.create_sheet("验收").append(["复核", 5])
        output = io.BytesIO()
        workbook.save(output)
        original = output.getvalue()
        file_id = self.upload("双表进度.xlsx", original, public=True)
        page = f"/files/{file_id}/preview"
        first = self.visitor.get(page)
        self.assertEqual(first.status_code, 200)
        self.assertIn("下一页".encode(), first.data)
        self.assertIn("事项 60".encode(), self.visitor.get(f"{page}?page=2").data)
        second_sheet = self.visitor.get(f"{page}?sheet=1")
        self.assertIn("复核".encode(), second_sheet.data)
        self.assertIn(b"<td>5</td>", second_sheet.data)
        with self.visitor.get(f"/files/{file_id}/download") as download:
            self.assertEqual(download.data, original)

    def test_markdown_render_source_and_original_agree(self):
        original = (
            "# 项目进度\n\n## 本周安排\n\n| 项目 | 状态 |\n| --- | --- |\n| 排版 | **进行中** |\n\n"
            "*普通正文*\n\n## 参考文献\n\n### 期刊\n\n*Journal Title*\n\n## 补充说明\n\n*补充内容*\n"
        ).encode()
        file_id = self.upload("进度说明.md", original, public=True)
        page = f"/files/{file_id}/preview"
        rendered = self.visitor.get(page)
        self.assertEqual(rendered.status_code, 200)
        self.assertIn("<h1>项目进度</h1>".encode(), rendered.data)
        self.assertIn(b"<table>", rendered.data)
        self.assertIn("<strong>进行中</strong>".encode(), rendered.data)
        self.assertIn('<em class="plain-emphasis">普通正文</em>'.encode(), rendered.data)
        self.assertIn(b"<em>Journal Title</em>", rendered.data)
        self.assertIn('<em class="plain-emphasis">补充内容</em>'.encode(), rendered.data)
        source = self.visitor.get(f"{page}?view=source")
        self.assertIn(original, source.data)
        self.assertNotIn(b"<table>", source.data)
        with self.visitor.get(f"/files/{file_id}/download") as download:
            self.assertEqual(download.data, original)

    def test_project_plan_replaces_intro_for_guests_and_members(self):
        cover = "HR2026年刊项目规划封面.webp"
        route = f"/files/intro/assets/{quote(cover)}"
        raw = INTRO_FILE.read_text(encoding="utf-8")
        self.assertEqual(INTRO_FILE.name, "HR2026年刊项目规划.md")
        self.assertIn(f"![HR2026年刊项目规划封面]({cover})", raw.replace("(./", "("))
        for client in (self.visitor, self.client):
            for path in ("/", "/files", "/files/intro"):
                with self.subTest(path=path, signed_in=client is self.client):
                    page = client.get(path)
                    self.assertEqual(page.status_code, 200)
                    self.assertEqual(page.headers["Cache-Control"], "no-store")
                    self.assertIn("HR2026年刊项目规划.md".encode(), page.data)
                    self.assertNotIn("小阿巴的留言".encode(), page.data)
                    self.assertRegex(page.data, (
                        f'<img [^>]*src="{route}"[^>]*loading="eager" decoding="async" '
                        'width="1191" height="1684" fetchpriority="high"'
                    ).encode())
                    self.assertIn(b"/static/files.css?v=7", page.data)
            source = client.get("/files/intro?view=source")
            self.assertIn(raw, unescape(source.get_data(as_text=True)))
            with client.get("/files/intro/download") as original:
                self.assertIn(quote(INTRO_FILE.name), original.headers["Content-Disposition"])
                self.assertEqual(original.data, INTRO_FILE.read_bytes())
        with self.visitor.get(route) as image:
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.mimetype, "image/webp")
            self.assertEqual(image.data, INTRO_FILE.with_name(cover).read_bytes())
            self.assertEqual(image.headers["Cache-Control"], "public, no-cache")
            etag = image.headers["ETag"]
            with Image.open(io.BytesIO(image.data)) as decoded:
                self.assertEqual(decoded.size, (1191, 1684))
        with self.visitor.get(route, headers={"If-None-Match": etag}) as cached:
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers["Cache-Control"], "public, no-cache")
            self.assertEqual(cached.data, b"")
        for name in ("窗口岗位.webp", "工作流程.svg", "文件夹怎么找.jpg", "跨章问题怎么提.jpg",
                     "文件谁能看.webp", "贡献怎么记.webp", "missing.webp"):
            with self.subTest(retired_asset=name):
                missing = self.visitor.get(f"/files/intro/assets/{quote(name)}")
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(missing.headers["Cache-Control"], "no-store")
        with self.visitor.get("/static/files.css?v=7") as stylesheet:
            self.assertNotIn(b"guide-headings-v1.woff2", stylesheet.data)


if __name__ == "__main__":
    unittest.main()
