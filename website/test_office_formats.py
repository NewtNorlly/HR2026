"""Verify that representative office files survive upload and render in Files."""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import test_app
from app import BASE, DATA, app, db
from openpyxl import Workbook
from test_release_assets import word_document


ODP_TYPE = "application/vnd.oasis.opendocument.presentation"
ODP_NAMESPACES = '''xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
  xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
  xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'''
ODP_STYLES = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles office:version="1.2" {ODP_NAMESPACES}>
  <office:styles/>
  <office:automatic-styles>
    <style:page-layout style:name="PageLayout">
      <style:page-layout-properties fo:page-width="28cm" fo:page-height="16cm"
        style:print-orientation="landscape"/>
    </style:page-layout>
  </office:automatic-styles>
  <office:master-styles>
    <style:master-page style:name="Default" style:page-layout-name="PageLayout"/>
  </office:master-styles>
</office:document-styles>
"""
ODP_CONTENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content office:version="1.2" {ODP_NAMESPACES}>
  <office:automatic-styles>
    <style:style style:name="SlideStyle" style:family="drawing-page"/>
    <style:style style:name="SlideText" style:family="paragraph">
      <style:text-properties fo:font-size="24pt"/>
    </style:style>
  </office:automatic-styles>
  <office:body><office:presentation>
    <draw:page draw:name="Progress" draw:style-name="SlideStyle" draw:master-page-name="Default">
      <draw:frame svg:x="2cm" svg:y="2cm" svg:width="23cm" svg:height="4cm">
        <draw:text-box><text:p text:style-name="SlideText">HR2026 office presentation</text:p></draw:text-box>
      </draw:frame>
    </draw:page>
    <draw:page draw:name="Review" draw:style-name="SlideStyle" draw:master-page-name="Default">
      <draw:frame svg:x="2cm" svg:y="2cm" svg:width="23cm" svg:height="4cm">
        <draw:text-box><text:p text:style-name="SlideText">HR2026 slide two for review</text:p></draw:text-box>
      </draw:frame>
    </draw:page>
  </office:presentation></office:body>
</office:document-content>
"""
ODP_MANIFEST = f"""<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
  <manifest:file-entry manifest:full-path="/" manifest:media-type="{ODP_TYPE}" manifest:version="1.2"/>
  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
  <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""


def presentation_bytes():
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("mimetype", ODP_TYPE, compress_type=ZIP_STORED)
        package.writestr("content.xml", ODP_CONTENT)
        package.writestr("styles.xml", ODP_STYLES)
        package.writestr("META-INF/manifest.xml", ODP_MANIFEST)
    return output.getvalue()


def workbook_bytes():
    book = Workbook()
    book.active.title = "项目进度"
    book.active.append(["Chapter", "Complete"])
    book.create_sheet("复核记录").append(["Reviewed files", 7])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def macro_enabled_workbook(plain):
    # A macro-enabled workbook can be saved before any VBA has been added.
    # Declare the proper OOXML content type without adding executable content.
    output = io.BytesIO()
    with ZipFile(io.BytesIO(plain)) as original, ZipFile(output, "w", ZIP_DEFLATED) as package:
        for name in original.namelist():
            data = original.read(name)
            if name == "[Content_Types].xml":
                data = data.replace(
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                )
            package.writestr(name, data)
    return output.getvalue()


class OfficeFormatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_app.SiteTests()
        self.fixture.setUp()
        self.assertEqual(self.fixture.login("lead").status_code, 302)
        self.visitor = app.test_client()

    def upload(self, extension, original, previous=None):
        fields = {"group": "1", "document": (io.BytesIO(original), f"样例-{extension}.{extension}")}
        if previous is not None:
            fields["previous"] = str(previous)
        response = self.fixture.post("/files/upload", fields)
        self.assertEqual(response.status_code, 302)
        return int(response.location.rsplit("file=", 1)[-1])

    def assert_original_download(self, client, file_id, original):
        with client.get(f"/files/{file_id}/download") as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
            self.assertEqual(response.data, original)

    def test_xlsx_and_xlsm_keep_sheet_tabs_and_open_cell_values(self):
        plain = workbook_bytes()
        for extension, original in (("xlsx", plain), ("xlsm", macro_enabled_workbook(plain))):
            with self.subTest(extension=extension):
                file_id = self.upload(extension, original)
                first_sheet = self.visitor.get(f"/files/{file_id}/preview")
                self.assertEqual(first_sheet.status_code, 200)
                self.assertIn("项目进度".encode(), first_sheet.data)
                self.assertIn(b"<td>Chapter</td>", first_sheet.data)
                second_sheet = self.visitor.get(f"/files?group=1&file={file_id}&sheet=1")
                self.assertEqual(second_sheet.status_code, 200)
                self.assertIn("复核记录".encode(), second_sheet.data)
                self.assertIn(b"<td>7</td>", second_sheet.data)
                self.assert_original_download(self.visitor, file_id, original)

    def test_browser_can_follow_preview_status_and_download_on_failure(self):
        original = word_document()
        file_id = self.upload("docx", original)
        with app.app_context(), db():
            db().execute("UPDATE files SET public=0 WHERE id=?", (file_id,))
        status_url = f"/files/{file_id}/preview-status"
        page_url = f"/files/{file_id}/preview"
        pending = self.visitor.get(page_url)
        self.assertEqual(pending.status_code, 200)
        self.assertIn(f'data-office-preview="{status_url}"'.encode(), pending.data)
        self.assertIn(b"office-preview.js", pending.data)
        status = self.visitor.get(status_url)
        self.assertEqual(status.get_json(), {"status": "pending"})
        self.assertEqual(status.headers["Cache-Control"], "no-store")
        with app.app_context(), db():
            db().execute("UPDATE files SET preview_status='failed' WHERE id=?", (file_id,))
        self.assertEqual(self.visitor.get(status_url).get_json(), {"status": "failed"})
        self.assertIn("无法生成预览".encode(), self.visitor.get(page_url).data)
        self.assert_original_download(self.visitor, file_id, original)
        result = self.fixture.post(f"/files/{file_id}/manage", {"action": "withdraw"})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(self.visitor.get(status_url).status_code, 404)

    def test_open_office_preview_moves_to_latest_render_status(self):
        original = word_document()
        first = self.upload("docx", original)
        with app.app_context(), db():
            db().execute("UPDATE files SET preview_status='failed' WHERE id=?", (first,))
        latest = self.upload("docx", original, previous=first)
        for client in (self.visitor, self.fixture.client):
            for action, target in (
                ("preview-status", f"/files/{latest}/preview-status"),
                ("inline", f"/files?group=1&file={latest}"),
            ):
                with self.subTest(action=action):
                    old = client.get(f"/files/{first}/{action}")
                    self.assertEqual((old.status_code, old.location), (302, target))
                    self.assertEqual(old.headers["Cache-Control"], "no-store")
            live_status = client.get(f"/files/{first}/preview-status", follow_redirects=True)
            self.assertEqual(live_status.get_json(), {"status": "pending"})

    def convert_fixture(self, source, output_filter, profile):
        extension = output_filter.split(":", 1)[0]
        run = subprocess.run(
            [shutil.which("soffice"), f"-env:UserInstallation={profile.as_uri()}", "--headless",
             "--convert-to", output_filter, "--outdir", str(source.parent), str(source)],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        output = source.with_suffix(f".{extension}")
        self.assertTrue(run.returncode == 0 and output.is_file(),
                        f"Fixture {extension} conversion failed: {run.stdout}\n{run.stderr}")
        return output.read_bytes()

    def create_pdf_fixtures(self, folder):
        word_source = folder / "letter.docx"
        word_source.write_bytes(word_document())
        sheet_source = folder / "table.xlsx"
        sheet_source.write_bytes(workbook_bytes())
        slides_source = folder / "slides.odp"
        slides_source.write_bytes(presentation_bytes())
        formats = {
            "docx": (word_source.read_bytes(), 1),
            "rtf": (rb"{\rtf1\ansi\deff0 HR2026 Rich Text Format preview\par}", 1),
            "odp": (slides_source.read_bytes(), 2),
        }
        for source, filters, page_count in (
            (word_source, ("doc:MS Word 97", "odt:writer8"), 1),
            (sheet_source, ("xls:MS Excel 97", "ods:calc8"), 1),
            (slides_source, ("ppt:MS PowerPoint 97", "pptx:Impress MS PowerPoint 2007 XML"), 2),
        ):
            for conversion in filters:
                extension = conversion.split(":", 1)[0]
                profile = folder / f"profile-{extension}"
                formats[extension] = (self.convert_fixture(source, conversion, profile), page_count)
        return formats

    def wait_for_worker(self, process, files):
        placeholders = ",".join("?" for _ in files)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and process.poll() is None:
            with app.app_context():
                rows = db().execute(
                    f"SELECT id,preview_status FROM files WHERE id IN ({placeholders})", list(files)
                ).fetchall()
            states = {row["id"]: row["preview_status"] for row in rows}
            if states.keys() == files.keys() and all(state in {"ready", "failed"} for state in states.values()):
                return states
            time.sleep(0.15)
        self.fail("Office preview queue did not finish")

    @unittest.skipUnless(shutil.which("soffice"), "LibreOffice (soffice) is not installed")
    def test_supported_office_families_render_through_background_worker(self):
        with tempfile.TemporaryDirectory(prefix="hr2026-formats-") as directory:
            folder = Path(directory)
            fixtures = self.create_pdf_fixtures(folder)
            files = {}
            for extension, (original, page_count) in fixtures.items():
                file_id = self.upload(extension, original)
                files[file_id] = (extension, original, page_count)
                if extension == "docx":
                    with app.app_context(), db():
                        db().execute("UPDATE files SET public=0 WHERE id=?", (file_id,))
                self.assertIn("正在生成预览".encode(), self.visitor.get(f"/files/{file_id}/preview").data)
                self.assertEqual(self.visitor.get(f"/files/{file_id}/inline").status_code, 404)

            log_file = folder / "worker.log"
            with log_file.open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "preview_worker"], cwd=BASE,
                    env={**os.environ, "HR2026_DATA": str(DATA)}, stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    states = self.wait_for_worker(process, files)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

            for file_id, (extension, original, page_count) in files.items():
                with self.subTest(extension=extension):
                    self.assertEqual(states[file_id], "ready", log_file.read_text(encoding="utf-8", errors="replace"))
                    status = self.visitor.get(f"/files/{file_id}/preview-status")
                    self.assertEqual(status.get_json().get("status"), "ready")
                    url = f"/files/{file_id}/inline"
                    preview = self.visitor.get(f"/files/{file_id}/preview")
                    self.assertEqual(preview.status_code, 200)
                    self.assertIn(f'src="{url}"'.encode(), preview.data)
                    with self.visitor.get(url) as pdf:
                        self.assertEqual(pdf.status_code, 200)
                        self.assertEqual(pdf.mimetype, "application/pdf")
                        self.assertTrue(pdf.data.startswith(b"%PDF-"))
                        self.assertTrue(pdf.data.rstrip().endswith(b"%%EOF"))
                        self.assertGreaterEqual(len(re.findall(rb"/Type\s*/Page\b", pdf.data)), page_count)
                    self.assert_original_download(self.visitor, file_id, original)


if __name__ == "__main__":
    unittest.main()
