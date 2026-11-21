import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from app import DATA, FILES, OFFICE


def valid_preview_pdf(path):
    try:
        with path.open("rb") as preview:
            if not re.fullmatch(rb"%PDF-[0-9]\.[0-9]", preview.read(8)):
                return False
            size = preview.seek(0, 2)
            preview.seek(max(8, size - 1024))
            trailer = re.search(rb"startxref\s+([0-9]{1,20})\s+%%EOF\s*\Z", preview.read())
            return bool(trailer and 8 <= int(trailer[1]) < size)
    except OSError:
        return False


def render(blob, extension):
    if not shutil.which("soffice"):
        raise OSError("LibreOffice is missing")
    with tempfile.TemporaryDirectory(prefix="hr2026-office-") as directory:
        work = Path(directory)
        original = work / f"document{extension}"
        shutil.copyfile(FILES / blob, original)
        subprocess.run(
            ["soffice", f"-env:UserInstallation={(work / 'profile').as_uri()}", "--headless",
             "--convert-to", "pdf", "--outdir", str(work), str(original)],
            check=True, timeout=100, capture_output=True,
        )
        generated = work / "document.pdf"
        if not valid_preview_pdf(generated):
            raise ValueError("LibreOffice did not generate a complete PDF preview")
        pending = DATA / "previews" / f"{blob}.tmp"
        shutil.copyfile(generated, pending)
        pending.replace(pending.with_suffix(".pdf"))


def worker():
    with sqlite3.connect(DATA / "project.sqlite3", timeout=15) as db:
        db.execute("UPDATE files SET preview_status='pending' WHERE preview_status='running'")
        placeholders = ",".join("?" for _ in OFFICE)
        ready = db.execute(f"SELECT id,blob FROM files WHERE preview_status='ready' AND ext IN ({placeholders})", tuple(OFFICE)).fetchall()
        for file_id, blob in ready:
            if not valid_preview_pdf(DATA / "previews" / f"{blob}.pdf"):
                db.execute("UPDATE files SET preview_status='pending' WHERE id=?", (file_id,))
    while True:
        with sqlite3.connect(DATA / "project.sqlite3", timeout=15) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,blob,ext FROM files WHERE preview_status='pending' AND withdrawn=0 ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE files SET preview_status='running' WHERE id=?", (row[0],))
        if not row:
            time.sleep(3)
            continue
        file_id, blob, extension = row
        try:
            render(blob, extension)
            result = "ready"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"Preview {file_id}: {error}", flush=True)
            result = "failed"
        with sqlite3.connect(DATA / "project.sqlite3", timeout=15) as db:
            db.execute("UPDATE files SET preview_status=? WHERE id=?", (result, file_id))


if __name__ == "__main__":
    worker()
