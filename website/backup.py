import os
import shutil
import sqlite3
import tarfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


data = Path(os.environ["HR2026_DATA"])
dest = Path(os.environ.get("HR2026_BACKUP", "/var/backups/hr2026"))
dest.mkdir(mode=0o700, parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
with TemporaryDirectory(dir=dest) as directory:
    staging = Path(directory)
    with closing(sqlite3.connect(data / "project.sqlite3")) as source, closing(sqlite3.connect(staging / "project.sqlite3")) as copy:
        source.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database backup integrity check failed")
        blobs = copy.execute("SELECT blob FROM files").fetchall()
        avatars = copy.execute("SELECT avatar FROM profiles WHERE avatar!=''").fetchall()
    (staging / "files").mkdir()
    for (blob,) in blobs:
        shutil.copyfile(data / "files" / blob, staging / "files" / blob)
    (staging / "avatars").mkdir()
    for (name,) in avatars:
        shutil.copyfile(data / "avatars" / name, staging / "avatars" / name)
    with (staging / "project.sqlite3").open("rb") as source:
        with tarfile.open(staging / "complete.tar.gz", "w:gz") as output:
            output.addfile(output.gettarinfo(str(staging / "project.sqlite3"), "project.sqlite3"), source)
            output.add(staging / "files", arcname="files")
            output.add(staging / "avatars", arcname="avatars")
    (staging / "complete.tar.gz").replace(dest / f"{stamp}.tar.gz")
for old in sorted(dest.glob("????????-??????.tar.gz"), reverse=True)[4:]:
    old.unlink()
