import getpass
import io
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import date
from functools import wraps
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import click
from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from markdown_it import MarkdownIt
from openpyxl import load_workbook
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash


BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("HR2026_DATA", BASE / "instance")).resolve()
FILES = DATA / "files"
AVATARS = DATA / "avatars"
for directory in (DATA, FILES, AVATARS, DATA / "previews"):
    directory.mkdir(parents=True, exist_ok=True)
PRODUCTION = os.environ.get("HR2026_PRODUCTION") == "1"
mimetypes.add_type("image/webp", ".webp")
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("HR2026_SECRET") or (None if PRODUCTION else "local-development-only"),
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=PRODUCTION, SESSION_COOKIE_SAMESITE="Lax",
)
if not app.secret_key:
    raise RuntimeError("Set HR2026_SECRET for production.")

STATES = {"todo": "没开始", "doing": "进行中", "waiting": "等反馈", "done": "已完成"}
RISKS = {"green": "正常", "yellow": "需跟进", "red": "有延期风险"}
ROLES = {"editor": "主编", "hr": "HR Manager", "lead": "组长", "liaison": "HR 联络员", "member": "组员"}
TABS = {"files": "项目文件", "task": "任务列表", "issues": "议题讨论", "wiki": "资料索引", "people": "成员名片"}
PUBLIC_ROSTER = os.environ.get("HR2026_PUBLIC_ROSTER") == "1"
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
OFFICE = set(".doc .docx .odt .rtf .wps .xls .ods .et .ppt .pptx .odp .dps".split())
SPREADSHEETS = {".xlsx", ".xlsm"}
MAX_FILE_FOLDER_DEPTH = 6
# A withdrawn tip is still the newest record. Never fall back to an older version.
LATEST_FILES = """WITH RECURSIVE latest_files(id,previous_id,group_id,latest_id) AS (
    SELECT f.id,f.previous_id,f.group_id,f.id FROM files f
    WHERE NOT EXISTS (SELECT 1 FROM files successor
                      WHERE successor.previous_id=f.id AND successor.group_id IS f.group_id)
    UNION ALL
    SELECT f.id,f.previous_id,f.group_id,latest_files.latest_id
    FROM latest_files JOIN files f ON f.id=latest_files.previous_id AND f.group_id IS latest_files.group_id
)"""
TEXT = set(".md .txt .csv .tsv .py .js .ts .tsx .jsx .json .html .css .sql .sh .c .cpp .h .java .go .rs .yaml .yml .xml .toml .ps1 .r .rb .php .vue .svelte .svg .log".split())
INTRO_FILE = BASE / "content" / "HR2026年刊项目规划.md"
INTRO_ASSETS = {
    "HR2026年刊项目规划封面.webp": ("image/webp", 1191, 1684),
}
REFERENCE_HEADINGS = {
    "参考资料", "参考文献", "参考书目", "参考资料与参考文献", "参考资料和参考文献",
    "references", "bibliography", "works cited",
}
MILESTONES = [
    ("组队与选题", "各章选题单和暂定节标题"),
    ("培训与采访", "首版采访整理稿，或公开材料和补访安排"),
    ("初稿与章节准备", "第 1 组交规划初稿，第 2–5 组交各自初稿，第 6 组交案例材料和提纲"),
    ("改稿与交叉审稿", "岗位与关系初稿、其余章节改稿、互审意见和滚动排版"),
    ("审稿、试读与校对", "经主编确认的最终 PDF"),
    ("印刷或发布", "成刊 PDF 和交接包"),
]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DATA / "project.sqlite3", timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def query(sql, params=()):
    return db().execute(sql, params).fetchall()


def one(sql, params=()):
    return db().execute(sql, params).fetchone()


def require(sql, params):
    row = one(sql, params)
    if row is None:
        abort(404)
    return row


def audit(event, detail, *, issue_id=None):
    db().execute("INSERT INTO audit(actor,event,detail,issue_id) VALUES(?,?,?,?)",
                 (g.user["name"], event, detail, issue_id))


def init_db():
    connection = db()
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript((BASE / "schema.sql").read_text(encoding="utf-8"))
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if "folder_id" not in {row["name"] for row in connection.execute("PRAGMA table_info(files)")}:
            connection.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER REFERENCES file_folders(id)")
        for table in ("issues", "comments"):
            if "deleted" not in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1))")
        if "issue_id" not in {row["name"] for row in connection.execute("PRAGMA table_info(audit)")}:
            connection.execute("ALTER TABLE audit ADD COLUMN issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL")
        connection.execute("""CREATE INDEX IF NOT EXISTS active_file_folder_name
                             ON files(group_id,name,folder_id,id) WHERE withdrawn=0""")


with app.app_context():
    init_db()


def form(name, maximum=120, required=True):
    value = request.form.get(name, "").strip()
    if len(value) > maximum or (required and not value):
        abort(400, f"请检查字段 {name}。")
    return value


def form_id(name, required=True):
    value = form(name, 12, required)
    if not value:
        return None
    if not value.isascii() or not value.isdigit():
        abort(400)
    return int(value)


def form_date(name, required=True):
    value = form(name, 10, required)
    if value:
        try:
            date.fromisoformat(value)
        except ValueError:
            abort(400, "请选择有效日期。")
    return value


def user_id(name, required=True):
    value = form_id(name, required)
    if value is not None:
        require("SELECT id FROM users WHERE id=?", (value,))
    return value


def is_admin():
    return g.user and g.user["role"] in ("editor", "hr")


def public_identity(name, person_id):
    if g.user or PUBLIC_ROSTER or person_id is None:
        return name
    return f"成员 {person_id:03d}"


def member(view):
    @wraps(view)
    def checked(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return checked


@app.before_request
def request_context():
    g.user = None
    if request.endpoint == "static":
        return
    if session.get("uid"):
        g.user = one("""SELECT u.*,p.bio,p.avatar FROM users u LEFT JOIN profiles p ON p.member_id=u.id
                     WHERE u.id=? AND u.auth_version=?""", (session["uid"], session.get("version")))
        if g.user is None:
            session.clear()
    if request.method == "POST":
        if PRODUCTION and not request.is_secure:
            abort(403, "请使用 HTTPS。")
        supplied = request.form.get("csrf", "")
        if not session.get("csrf") or not secrets.compare_digest(supplied, session["csrf"]):
            abort(400, "页面已过期，请刷新后重试。")
    if g.user and g.user["must_change"] and request.endpoint not in {"password", "logout", "static"}:
        return redirect(url_for("password"))


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'; frame-ancestors 'self'")
    if request.endpoint == "intro_asset" and response.status_code in (200, 206, 304):
        response.headers["Cache-Control"] = "public, no-cache"
    elif request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.context_processor
def template_helpers():
    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(24)
        return session["csrf"]
    default_avatar_url = url_for("static", filename="researcher-avatar.webp", v="4")

    def member_avatar_url(person_id, picture):
        if picture and (g.user or PUBLIC_ROSTER) and (AVATARS / picture).is_file():
            return url_for("avatar", person_id=person_id)
        return default_avatar_url

    return dict(csrf=csrf, states=STATES, risks=RISKS, roles=ROLES, tabs=TABS, admin=is_admin(),
                public_identity=public_identity, roster_public=PUBLIC_ROSTER,
                default_avatar_url=default_avatar_url, member_avatar_url=member_avatar_url)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
@app.errorhandler(429)
def show_error(error):
    return render_template("error.html", code=error.code, message=error.description), error.code


@app.get("/")
def home():
    return files_view(0, show_intro=True)


@app.get("/health")
def health():
    one("SELECT 1")
    return {"status": "ok"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if PRODUCTION and not request.is_secure:
        return render_template("error.html", code="HTTPS", message="成员登录将在 HTTPS 入口开放后启用。"), 403
    if request.method == "POST":
        identity = form("username", 65)
        password_value = form("password", 256)
        now = int(time.time())
        limit_key = request.remote_addr or "unknown"
        limit = one("SELECT * FROM login_limits WHERE key=?", (limit_key,))
        if limit and limit["until"] > now and limit["failures"] >= 20:
            abort(429, "尝试次数过多，请稍后重试。")
        account = one("SELECT * FROM users WHERE username=?", (identity.lower(),))
        if account is None:
            matches = query("SELECT * FROM users WHERE name=? LIMIT 2", (identity,))
            account = matches[0] if len(matches) == 1 else None
        if not account or not check_password_hash(account["password"], password_value):
            with db():
                db().execute("""INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET
                             failures=CASE WHEN until>? THEN failures+1 ELSE 1 END,
                             until=CASE WHEN until>? THEN until ELSE excluded.until END""",
                             (limit_key, now + 600, now, now))
                db().execute("DELETE FROM login_limits WHERE until<?", (now,))
            flash("账号或密码有误。", "error")
        else:
            with db():
                db().execute("DELETE FROM login_limits WHERE key=?", (limit_key,))
            session.clear()
            session.update(uid=account["id"], version=account["auth_version"])
            return redirect(url_for("password" if account["must_change"] else "home"))
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/password", methods=["GET", "POST"])
@member
def password():
    if request.method == "POST":
        current = form("current", 256)
        updated = form("new", 256)
        if len(updated) < 12 or updated == current or updated != form("confirm", 256):
            flash("请设置至少 12 字符的新密码，并确认两次输入一致。", "error")
        elif not check_password_hash(g.user["password"], current):
            flash("当前密码有误。", "error")
        else:
            with db():
                db().execute("UPDATE users SET password=?,must_change=0,auth_version=auth_version+1 WHERE id=?", (generate_password_hash(updated), g.user["id"]))
            session.clear()
            session.update(uid=g.user["id"], version=g.user["auth_version"] + 1)
            if g.user["must_change"]:
                flash("新密码已保存。你可以换一张自己的头像，再写几句个人感想放进 People 名片。", "profile-onboarding")
            return redirect(url_for("home"))
    return render_template("auth.html", mode="password")


def account_fields(username, name, password_value, role, group, initials=""):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,39}", username) or not 12 <= len(password_value) <= 256 or not name:
        raise ValueError("账号用 2–40 个小写字母、数字或 ._-，密码至少 12 字符。")
    if role not in ROLES or (role in ("editor", "hr")) != (group is None):
        raise ValueError("请选择与岗位对应的小组。")
    return username, name, generate_password_hash(password_value), role, group, initials


@app.cli.command("create-owner")
@click.option("--username", default="admin")
@click.option("--name", default="主编")
def create_owner(username, name):
    """Create the initial editor account and its six milestones."""
    if one("SELECT id FROM users WHERE role='editor'"):
        raise click.ClickException("主编账号已存在。")
    value = getpass.getpass("设置初始主编密码（至少 12 字符）: ")
    if value != getpass.getpass("再次输入: "):
        raise click.ClickException("两次输入不同。")
    try:
        fields = account_fields(username, name, value, "editor", None)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with db():
        editor = db().execute("INSERT INTO users(username,name,password,role,group_id,initials) VALUES(?,?,?,?,?,?)", fields).lastrowid
        db().executemany("INSERT INTO tasks(title,week,owner_id,reviewer_id,due,deliverable) VALUES(?,?,?,?,?,?)",
                         [(title, week, editor, editor, "", deliverable) for week, (title, deliverable) in enumerate(MILESTONES, 13)])
    click.echo("主编账号已建立。首次登录需要修改密码。")


def all_people():
    return query("SELECT id,name,role,group_id FROM users ORDER BY initials COLLATE NOCASE, name")


def get_tasks():
    return query(LATEST_FILES + """ SELECT t.*, o.name owner, r.name reviewer, c.name collaborator, w.name waiting_name,
                f.id latest_file_id, f.group_id latest_file_group_id, f.name file_name
                FROM tasks t JOIN users o ON t.owner_id=o.id JOIN users r ON t.reviewer_id=r.id
                LEFT JOIN users c ON t.collaborator_id=c.id LEFT JOIN users w ON t.waiting_for=w.id
                LEFT JOIN latest_files ON t.file_id=latest_files.id
                LEFT JOIN files f ON f.id=latest_files.latest_id AND f.withdrawn=0 ORDER BY t.week,t.parent_id,t.id""")


def may_edit_task_scope(group_id):
    if not g.user:
        return False
    if group_id is None:
        return is_admin()
    return g.user["role"] == "lead" and g.user["group_id"] == group_id


TASK_TRANSITIONS = {
    "todo": ("todo", "doing", "waiting"),
    "doing": ("doing", "waiting", "done"),
    "waiting": ("waiting", "doing", "done"),
    "done": ("done", "doing"),
}


def task_progress_options(record):
    if not g.user:
        return ()
    manager = may_edit_task_scope(record["group_id"])
    reviewer = record["reviewer_id"] == g.user["id"]
    owner = record["owner_id"] == g.user["id"]
    if not (manager or reviewer or (owner and record["status"] != "done")):
        return ()
    return tuple(state for state in TASK_TRANSITIONS[record["status"]] if state != "done" or reviewer)


def task_has_visible_file(record):
    return bool(record["file_name"])


def task_outline_lists(tasks, groups):
    """Keep legacy cross-list links without placing a Group task in Officer."""
    by_id = {record["id"]: record for record in tasks}
    roots = {group["id"]: [] for group in groups}
    children = {}
    for record in tasks:
        parent = by_id.get(record["parent_id"])
        if parent is not None and parent["group_id"] == record["group_id"]:
            children.setdefault(parent["id"], []).append(record)
        else:
            roots[record["group_id"]].append(record)
    return roots, children, by_id


@app.get("/task")
@app.get("/task/data", endpoint="task_data")
def task():
    tasks = get_tasks()
    week = request.args.get("week", type=int)
    if week not in range(13, 19):
        week = None
    groups = [{"id": None, "title": "Officer", "chapters": "全刊工程"}]
    groups.extend({"id": group["id"], "title": f"Group {group['id']}", "chapters": group["chapters"]}
                  for group in query("SELECT id,chapters FROM groups ORDER BY id"))
    current = [record for record in tasks if week is None or record["week"] == week]
    roots, children, by_id = task_outline_lists(current, groups)

    if request.endpoint == "task_data":
        def task_payload(record):
            return {
                "id": record["id"], "title": record["title"], "week": record["week"],
                "assignee": public_identity(record["owner"], record["owner_id"]),
                "due": record["due"] or None, "status": record["status"], "risk": record["risk"],
                "children": [task_payload(child) for child in children.get(record["id"], ())],
            }

        return {"lists": [{**group, "outlines": [task_payload(record) for record in roots[group["id"]]]}
                          for group in groups]}

    related = {}
    if g.user:
        for issue in query("SELECT id,task_id,title,resolution FROM issues WHERE task_id IS NOT NULL AND deleted=0 ORDER BY id"):
            related.setdefault(issue["task_id"], []).append(issue)
    return render_template(
        "task.html", groups=groups, roots=roots, children=children, by_id=by_id, all_tasks=tasks,
        week=week, focus=request.args.get("focus", type=int),
        people=all_people() if g.user else (), can_edit=may_edit_task_scope,
        progress_options=task_progress_options, visible_file=task_has_visible_file, related=related,
    )


@app.post("/task/save")
@member
def save_task():
    record_id = form_id("id", False)
    parent_id = form_id("parent", False)
    group_id = g.user["group_id"]
    if not may_edit_task_scope(group_id):
        abort(403)
    title = form("title")
    due = form_date("due")
    deliverable = form("deliverable", 800)
    with db():
        db().execute("BEGIN IMMEDIATE")
        existing = require("SELECT * FROM tasks WHERE id=?", (record_id,)) if record_id is not None else None
        if existing is not None and existing["group_id"] != group_id:
            abort(403)
        if parent_id is None:
            week = form_id("week")
        else:
            parent = require("SELECT * FROM tasks WHERE id=?", (parent_id,))
            if record_id == parent_id or parent["parent_id"] is not None:
                abort(400, "细则只能放在纲要下。")
            keeping_legacy_parent = existing is not None and existing["parent_id"] == parent_id
            same_list = parent["group_id"] == group_id
            if not (same_list or keeping_legacy_parent):
                abort(403, "细则与纲要须在同一列表。")
            if parent["status"] == "done":
                abort(409, "请先重新打开对应的纲要。")
            week = parent["week"]
        if week not in range(13, 19):
            abort(400, "请选择 W13–W18 的周次。")

        owner, reviewer, collaborator = user_id("owner"), user_id("reviewer"), user_id("collaborator", False)
        if group_id is not None:
            for person in (owner, reviewer):
                if not one("SELECT id FROM users WHERE id=? AND group_id=?", (person, group_id)):
                    abort(400, "组内任务的负责人和验收人须为本组成员。")
        elif not one("SELECT id FROM users WHERE id=? AND role IN ('editor','hr')", (reviewer,)):
            abort(400, "Officer 的任务由主编或 HR Manager 验收。")
        values = (title, week, group_id, parent_id, owner, reviewer, collaborator, due, deliverable)
        if existing is not None:
            if existing["status"] == "done":
                abort(409, "请先重新打开已完成任务。")
            if (parent_id is not None or week != existing["week"]) and one("SELECT id FROM tasks WHERE parent_id=?", (record_id,)):
                abort(409, "纲要包含细则时，请保留纲要层级和周次。")
            db().execute("""UPDATE tasks SET title=?,week=?,group_id=?,parent_id=?,owner_id=?,reviewer_id=?,
                          collaborator_id=?,due=?,deliverable=? WHERE id=?""", (*values, record_id))
        else:
            record_id = db().execute("""INSERT INTO tasks(title,week,group_id,parent_id,owner_id,reviewer_id,
                                     collaborator_id,due,deliverable) VALUES(?,?,?,?,?,?,?,?,?)""", values).lastrowid
        audit("更新任务" if existing is not None else "新建任务", title)
    flash("任务已保存。", "success")
    return redirect(url_for("task", week=week, focus=record_id, _anchor=f"task-{record_id}"))


@app.post("/task/<int:task_id>/progress")
@member
def task_progress(task_id):
    with db():
        db().execute("BEGIN IMMEDIATE")
        record = require("SELECT * FROM tasks WHERE id=?", (task_id,))
        options = task_progress_options(record)
        if not options:
            abort(403)
        status, risk = form("status", 12), form("risk", 12)
        if risk not in RISKS or status not in TASK_TRANSITIONS[record["status"]]:
            abort(400, "请选择当前任务允许的状态。")
        if status not in options:
            abort(403, "由指定验收人确认完成。")
        if status == "done" and one("SELECT id FROM tasks WHERE parent_id=? AND status!='done'", (task_id,)):
            abort(409, "还有未验收的组内细则。")
        if status != "done" and record["parent_id"]:
            if one("SELECT id FROM tasks WHERE id=? AND status='done'", (record["parent_id"],)):
                abort(409, "请先重新打开对应的大纲。")
        file_id = form_id("file_id", False)
        if file_id:
            require("SELECT id FROM files WHERE id=? AND withdrawn=0", (file_id,))
        wait_id = user_id("waiting_for") if status == "waiting" else None
        blocker = form("blocker", 800, required=status == "waiting" or risk != "green")
        reply_due = form_date("reply_due", required=status == "waiting")
        db().execute("UPDATE tasks SET status=?,risk=?,file_id=?,waiting_for=?,blocker=?,reply_due=? WHERE id=?",
                     (status, risk, file_id, wait_id, blocker, reply_due, task_id))
        if status == "done" and record["status"] != "done":
            db().execute("INSERT INTO contributions(member_id,kind,detail,evidence,status,file_id,task_id) VALUES(?,?,?,?,?,?,?)",
                         (record["owner_id"], "deliverable", record["title"], "", "approved", file_id, task_id))
        if status != "done" and record["status"] == "done":
            db().execute("""UPDATE contributions SET status='reopened'
                          WHERE task_id=? AND kind='deliverable' AND status='approved'""", (task_id,))
        audit("任务进度", f"{record['title']} · {STATES[status]}")
    flash("任务进度已更新。", "success")
    return redirect(url_for("task", week=record["week"], focus=task_id, _anchor=f"task-{task_id}"))


def may_write_file(group):
    return bool(g.user and (group == g.user["group_id"] if group is not None else g.user["role"] in ("editor", "hr", "lead")))


def may_manage_file(record):
    return may_organize_root(record["group_id"])


def may_organize_root(group):
    if not g.user:
        return False
    role = g.user["role"]
    if group is None:
        return role in ("editor", "hr")
    return role == "editor" or (role == "lead" and group == g.user["group_id"])


def may_update_file(record):
    return may_write_file(record["group_id"]) and (record["group_id"] is not None or record["owner_id"] == g.user["id"])


def file_row(file_id):
    if not 1 <= file_id <= 999_999_999_999:
        abort(404)
    return require("SELECT * FROM files WHERE id=? AND withdrawn=0", (file_id,))


def latest_public_file(file_id):
    if not 1 <= file_id <= 999_999_999_999:
        return None
    return one("""WITH RECURSIVE versions(id,group_id) AS (
                       SELECT id,group_id FROM files WHERE id=? AND withdrawn=0
                       UNION
                       SELECT f.id,f.group_id FROM files f JOIN versions v
                       ON f.previous_id=v.id AND f.group_id IS v.group_id
                   ) SELECT newest.* FROM versions v JOIN files newest ON newest.id=v.id
                   WHERE newest.withdrawn=0 AND NOT EXISTS (
                       SELECT 1 FROM files successor
                       WHERE successor.previous_id=v.id AND successor.group_id IS v.group_id
                   )""", (file_id,))


def public_file_or_404(file_id):
    record = latest_public_file(file_id)
    if record is None:
        abort(404)
    return record


def public_file_view_options(record, *, allow_embedded=False):
    options = {}
    if record["ext"] == ".md" and request.args.get("view") == "source":
        options["view"] = "source"
    if record["ext"] in SPREADSHEETS:
        sheet = request.args.get("sheet", type=int)
        page = request.args.get("page", type=int)
        if sheet is not None:
            options["sheet"] = max(0, sheet)
        if page is not None:
            options["page"] = max(1, min(1000, page))
    if allow_embedded and request.args.get("embedded") in ("1", "true"):
        options["embedded"] = request.args["embedded"]
    return options


def file_folder(folder_id):
    if not 1 <= folder_id <= 999_999_999_999:
        abort(404)
    return require("SELECT * FROM file_folders WHERE id=?", (folder_id,))


def file_folder_in_root(folder_id, group):
    if folder_id is None:
        return None
    folder = file_folder(folder_id)
    if folder["group_id"] != group:
        abort(404)
    return folder


def folder_lineage(folder, lookup):
    path = []
    while folder is not None and len(path) < MAX_FILE_FOLDER_DEPTH:
        path.append(folder)
        parent_id = folder["parent_id"]
        folder = lookup(parent_id) if parent_id is not None else None
    if folder is not None:
        abort(404)
    return list(reversed(path))


def file_folder_tree():
    folders = {row["id"]: dict(row, children=[])
               for row in query("SELECT * FROM file_folders ORDER BY name_key,id")}
    roots = {number: [] for number in range(7)}
    for folder in folders.values():
        parent_id = folder["parent_id"]
        children = roots[folder["group_id"] or 0] if parent_id is None else folders[parent_id]["children"]
        children.append(folder)
    return roots, folders


def describe_folders(folders, root_label):
    for folder in folders:
        label = f"{root_label} / {folder['name']}"
        yield {"id": folder["id"], "label": label}
        yield from describe_folders(folder["children"], label)


def files_view(group, selected_id=None, folder_id=None, show_intro=False):
    if group not in range(7):
        abort(404)
    selected = public_file_or_404(selected_id) if selected_id else None
    if selected and selected["group_id"] != (group or None):
        abort(404)
    if selected:
        if selected_id != selected["id"] or "folder" in request.args or "group" not in request.args:
            return redirect(url_for("files", group=group, file=selected["id"], **public_file_view_options(selected)))
        folder_id = selected["folder_id"]
    folder_tree, folders = file_folder_tree()
    folder = folders.get(folder_id)
    if folder_id is not None and (folder is None or folder["group_id"] != (group or None)):
        abort(404)
    folder_path = folder_lineage(folder, folders.get)

    content = query("""SELECT f.*,u.name author FROM files f JOIN users u ON f.owner_id=u.id
                       WHERE f.withdrawn=0
                       AND NOT EXISTS (SELECT 1 FROM files n WHERE n.previous_id=f.id AND n.group_id IS f.group_id)
                       ORDER BY f.group_id,f.name COLLATE NOCASE,f.id DESC""")
    tree = {number: [] for number in range(7)}
    files_by_folder = {folder_id: [] for folder_id in folders}
    for record in content:
        if record["folder_id"] is None:
            tree[record["group_id"] or 0].append(record)
        else:
            files_by_folder[record["folder_id"]].append(record)
    current_folders = folder["children"] if folder else folder_tree[group]
    current_files = files_by_folder[folder_id] if folder else tree[group]
    can_organize = may_organize_root(group or None)
    folder_destinations = []
    if can_organize and selected:
        root_label = f"Group {group}" if group else "Officer"
        folder_destinations = [{"id": 0, "label": root_label}, *describe_folders(folder_tree[group], root_label)]

    options = {}
    if selected is not None:
        options = file_preview(selected, embedded_in_files=True)
    elif show_intro and INTRO_FILE.is_file():
        raw = INTRO_FILE.read_text(encoding="utf-8")
        selected = {"name": INTRO_FILE.name, "ext": ".md"}
        options = markdown_preview(raw, bundled=True)
        options["download_url"] = url_for("download_intro")
    return render_template(
        "files.html", group=group, tree=tree, groups=query("SELECT * FROM groups ORDER BY id"), selected=selected,
        intro=bool(show_intro and selected), has_intro=INTRO_FILE.is_file(), intro_name=INTRO_FILE.name,
        writable=may_write_file(group or None), can_manage=may_manage_file, can_update=may_update_file, **options,
        folder=folder, folder_path=folder_path, folder_tree=folder_tree, files_by_folder=files_by_folder,
        current_folders=current_folders, current_files=current_files, folder_destinations=folder_destinations,
        can_organize=can_organize, max_folder_depth=MAX_FILE_FOLDER_DEPTH,
    )


@app.get("/files")
def files():
    selected_id = request.args.get("file", type=int)
    group = request.args.get("group", default=0, type=int)
    folder_id = request.args.get("folder", type=int)
    if "folder" in request.args and (folder_id is None or folder_id < 0):
        abort(404)
    show_intro = group == 0 and not selected_id and not folder_id and "group" not in request.args
    return files_view(group, selected_id, folder_id or None, show_intro=show_intro)


@app.get("/files/intro")
def intro():
    return files_view(0, show_intro=True)


@app.get("/files/intro/download")
def download_intro():
    return send_file(INTRO_FILE, as_attachment=True, download_name=INTRO_FILE.name, mimetype="text/markdown")


@app.get("/files/intro/assets/<name>")
def intro_asset(name):
    asset = INTRO_ASSETS.get(name)
    if asset is None:
        abort(404)
    return send_file(BASE / "content" / name, mimetype=asset[0])


def folder_name():
    name = unicodedata.normalize("NFC", form("name", 80))
    key = unicodedata.normalize("NFKC", name).strip().casefold()
    if not key or key in (".", "..") or any(c in "/\\" or unicodedata.category(c).startswith("C") for c in key):
        abort(400, "请输入有效的文件夹名称，名称中不能含斜杠或控制字符。")
    return name, key


@app.post("/files/folders")
@member
def folder_create():
    group = form_id("group")
    if group not in range(7):
        abort(404)
    root = group or None
    if not may_organize_root(root):
        abort(403)
    name, key = folder_name()
    parent_id = form_id("parent", False) or None
    with db():
        db().execute("BEGIN IMMEDIATE")
        parent = file_folder_in_root(parent_id, root)
        path = folder_lineage(parent, lambda item: file_folder_in_root(item, root))
        if len(path) >= MAX_FILE_FOLDER_DEPTH:
            abort(400, f"子文件夹最多可建 {MAX_FILE_FOLDER_DEPTH} 层。")
        try:
            folder_id = db().execute("INSERT INTO file_folders(group_id,parent_id,name,name_key) VALUES(?,?,?,?)",
                                     (root, parent_id, name, key)).lastrowid
        except sqlite3.IntegrityError:
            abort(409, "此处已有同名文件夹。")
        audit("新建文件夹", name)
    flash("文件夹已创建。", "success")
    return redirect(url_for("files", group=group, folder=folder_id))


@app.post("/files/folders/<int:folder_id>/rename")
@member
def folder_rename(folder_id):
    with db():
        db().execute("BEGIN IMMEDIATE")
        folder = file_folder(folder_id)
        if not may_organize_root(folder["group_id"]):
            abort(403)
        name, key = folder_name()
        if name != folder["name"]:
            try:
                db().execute("UPDATE file_folders SET name=?,name_key=? WHERE id=?", (name, key, folder_id))
            except sqlite3.IntegrityError:
                abort(409, "此处已有同名文件夹。")
            audit("重命名文件夹", f"{folder['name']} → {name}")
    flash("文件夹名称已保存。", "success")
    return redirect(url_for("files", group=folder["group_id"] or 0, folder=folder_id))


@app.post("/files/<int:file_id>/move")
@member
def move_file(file_id):
    with db():
        db().execute("BEGIN IMMEDIATE")
        record = file_row(file_id)
        group = record["group_id"]
        if not may_organize_root(group):
            abort(403)
        folder_id = form_id("folder", False) or None
        destination = file_folder_in_root(folder_id, group)
        if folder_id != record["folder_id"]:
            db().execute("""WITH RECURSIVE ancestors(id,previous_id) AS (
                                SELECT id,previous_id FROM files WHERE id=?
                                UNION SELECT f.id,f.previous_id FROM files f JOIN ancestors a ON f.id=a.previous_id
                                WHERE f.group_id IS ?
                            ), versions(id) AS (
                                SELECT id FROM ancestors WHERE previous_id IS NULL
                                UNION SELECT f.id FROM files f JOIN versions v ON f.previous_id=v.id
                                WHERE f.group_id IS ?
                            )
                            UPDATE files SET folder_id=? WHERE id IN (SELECT id FROM versions)""",
                         (file_id, group, group, folder_id))
            audit("移动文件", f"{record['name']} → {destination['name'] if destination else '顶层文件夹'}")
        newest = latest_public_file(file_id)
    flash("文件和历史版本已移到所选文件夹。", "success")
    if newest is None:
        return redirect(url_for("files", group=group or 0, folder=folder_id))
    return redirect(url_for("files", group=group or 0, file=newest["id"]))


@app.post("/files/upload")
@member
def upload():
    group = form_id("group") or None
    if not may_write_file(group):
        abort(403)
    folder_id = form_id("folder", False) or None
    upload_file = request.files.get("document")
    if not upload_file:
        abort(400, "请选择文件。")
    name = upload_file.filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or len(name) > 180 or any(ord(c) < 32 for c in name):
        abort(400, "文件名无效。")
    ext = Path(name).suffix.lower()
    if not group and name == INTRO_FILE.name:
        abort(409, "这个文件名已用于项目说明。")
    if ext not in TEXT | OFFICE | set(IMAGES) | SPREADSHEETS | {".pdf"}:
        abort(400, "此文件格式没有站内预览，请使用页面列出的格式。")
    previous = form_id("previous", False)
    blob = uuid4().hex
    path = FILES / blob
    try:
        upload_file.save(path)
        with db():
            db().execute("BEGIN IMMEDIATE")
            if previous:
                old = file_row(previous)
                if group != old["group_id"] or not may_update_file(old):
                    abort(403)
                if one("SELECT id FROM files WHERE previous_id=?", (previous,)):
                    abort(409, "这个版本已有后续更新。")
                folder_id = old["folder_id"]
            file_folder_in_root(folder_id, group)
            size = path.stat().st_size
            if not size:
                abort(400, "文件为空。")
            if one("SELECT COALESCE(SUM(size),0) total FROM files")["total"] + size > 5 * 1024 ** 3:
                abort(409, "项目文件已达到当前存储额度，请联系站点管理员。")
            new = db().execute("""INSERT INTO files(previous_id,name,blob,ext,size,group_id,folder_id,owner_id,public,preview_status)
                                 VALUES(?,?,?,?,?,?,?,?,1,?)""",
                               (previous, name, blob, ext, size, group, folder_id, g.user["id"],
                                "pending" if ext in OFFICE else "ready")).lastrowid
            audit("更新文件版本" if previous else "上传文件", name)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    flash("文件已上传。" if ext not in OFFICE else "文件已上传，正在生成站内预览。", "success")
    return redirect(url_for("files", group=group or 0, file=new))


@app.post("/files/<int:file_id>/manage")
@member
def manage_file(file_id):
    record = file_row(file_id)
    if not may_manage_file(record):
        abort(403)
    if form("action", 20) != "withdraw":
        abort(400)
    with db():
        db().execute("BEGIN IMMEDIATE")
        record = file_row(file_id)
        db().execute("UPDATE files SET withdrawn=1,public=0 WHERE id=?", (file_id,))
        audit("撤下文件", record["name"])
    return redirect(url_for("files", group=record["group_id"] or 0, folder=record["folder_id"]))


@app.get("/files/<int:file_id>/download")
def download(file_id):
    record = public_file_or_404(file_id)
    if record["id"] != file_id:
        return redirect(url_for("download", file_id=record["id"]))
    return send_file(FILES / record["blob"], as_attachment=True, download_name=record["name"], mimetype="application/octet-stream")


def office_pdf_path(record):
    return DATA / "previews" / f"{record['blob']}.pdf"


def office_preview_status(record):
    status = record["preview_status"]
    if status == "ready" and not office_pdf_path(record).is_file():
        return "failed"
    return status


@app.get("/files/<int:file_id>/preview-status")
def preview_status(file_id):
    record = public_file_or_404(file_id)
    if record["id"] != file_id:
        return redirect(url_for("preview_status", file_id=record["id"]))
    if record["ext"] not in OFFICE:
        abort(404)
    return {"status": office_preview_status(record)}


@app.get("/files/<int:file_id>/inline")
def inline(file_id):
    record = public_file_or_404(file_id)
    if record["id"] != file_id:
        if not (record["ext"] in IMAGES or record["ext"] == ".pdf"
                or record["ext"] in OFFICE and office_preview_status(record) == "ready"):
            return redirect(url_for("files", group=record["group_id"] or 0, file=record["id"]))
        return redirect(url_for("inline", file_id=record["id"]))
    if record["ext"] in IMAGES:
        return send_file(FILES / record["blob"], mimetype=IMAGES[record["ext"]])
    if record["ext"] == ".pdf":
        return send_file(FILES / record["blob"], mimetype="application/pdf")
    if record["ext"] in OFFICE and office_preview_status(record) == "ready":
        return send_file(office_pdf_path(record), mimetype="application/pdf")
    abort(404)


def embedded_image_url(src, record, bundled):
    parsed = urlsplit(src)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = unquote(parsed.path)
    if bundled:
        filename = path.removeprefix("./")
        return url_for("intro_asset", name=filename) if filename in INTRO_ASSETS else None
    match = re.fullmatch(r"/files/(\d{1,12})/inline", path)
    if match:
        img = latest_public_file(int(match[1]))
        if img and img["ext"] in IMAGES:
            return url_for("inline", file_id=img["id"])
        return None
    filename = path.removeprefix("./")
    if not filename or "/" in filename or "\\" in filename:
        return None
    image_candidates = LATEST_FILES + """ SELECT MAX(newest.id) id,newest.ext
              FROM files named JOIN latest_files ON latest_files.id=named.id
              JOIN files newest ON newest.id=latest_files.latest_id
              WHERE named.name=? AND named.group_id IS ? AND named.withdrawn=0 AND newest.withdrawn=0"""
    group_by_folder = " GROUP BY newest.folder_id"
    img = one(image_candidates + " AND newest.folder_id IS ?" + group_by_folder,
              (filename, record["group_id"], record["folder_id"]))
    if img is None:
        # Preserve basename links when a document or image moves by itself.
        candidates = query(image_candidates + group_by_folder + " LIMIT 2", (filename, record["group_id"]))
        if len(candidates) == 1:
            img = candidates[0]
    if img and img["ext"] in IMAGES:
        return url_for("inline", file_id=img["id"])
    return None


def markdown_preview(raw, record=None, bundled=False):
    mode = "source" if request.args.get("view") == "source" else "render"
    result = {"text": raw, "mode": mode, "markdown_file": True}
    if mode == "source":
        return result
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = md.parse(raw)
    headings = []
    reference_level = None
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            level = int(token.tag[1:])
            if reference_level is not None and level <= reference_level:
                reference_level = None
            title = tokens[i + 1].content.strip().rstrip(":：").casefold()
            if reference_level is None and title in REFERENCE_HEADINGS:
                reference_level = level
        if token.type == "heading_open" and token.tag in ("h2", "h3"):
            name = tokens[i + 1].content
            slug = f"section-{len(headings) + 1}"
            token.attrSet("id", slug)
            headings.append({"text": name, "id": slug, "level": token.tag})
        if token.type == "inline":
            for child in token.children or []:
                if child.type == "em_open" and reference_level is None:
                    child.attrSet("class", "plain-emphasis")
                if child.type == "image":
                    original_src = child.attrGet("src") or ""
                    try:
                        src = embedded_image_url(original_src, record, bundled)
                    except ValueError:
                        src = None
                    if src:
                        child.attrSet("src", src)
                        child.attrSet("loading", "lazy")
                        child.attrSet("decoding", "async")
                        if bundled:
                            filename = unquote(urlsplit(original_src).path).removeprefix("./")
                            _, width, height = INTRO_ASSETS[filename]
                            child.attrSet("width", str(width))
                            child.attrSet("height", str(height))
                            if filename == "HR2026年刊项目规划封面.webp":
                                child.attrSet("loading", "eager")
                                child.attrSet("fetchpriority", "high")
                    else:
                        child.type = "text"
                        child.content = child.content or "图片不可用"
    return {**result, "markdown": md.renderer.render(tokens, md.options, {}), "headings": headings}


def file_preview(record, embedded_in_files=False):
    file_id = record["id"]
    ext = record["ext"]
    office_status = office_preview_status(record) if ext in OFFICE else None
    options = {
        "file": record, "inline": ext in set(IMAGES) | {".pdf"} or office_status == "ready", "image": ext in IMAGES,
        "office_status": office_status, "office_status_url": url_for("preview_status", file_id=file_id) if office_status else None,
        "download_url": url_for("download", file_id=file_id), "inline_url": url_for("inline", file_id=file_id),
    }
    if embedded_in_files:
        options["preview_url"] = url_for("files", group=record["group_id"] or 0, file=file_id)
    else:
        options["preview_url"] = url_for("preview", file_id=file_id, embedded=request.args.get("embedded"))
    path = FILES / record["blob"]
    if ext in TEXT:
        with path.open("rb") as src:
            data = src.read(512 * 1024 + 1)
        options.update(text=data[:512 * 1024].decode("utf-8", "replace"), truncated=len(data) > 512 * 1024)
        if ext == ".md":
            options.update(markdown_preview(options["text"], record))
    if ext in SPREADSHEETS:
        book = None
        try:
            from zipfile import ZipFile
            with ZipFile(path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 96 * 1024 * 1024:
                    raise ValueError("Workbook exceeds preview decompression budget")
            with path.open("rb") as src:
                book = load_workbook(src, read_only=True, data_only=True, keep_links=False)
                sheets = book.sheetnames
                sheet_number = max(0, min(request.args.get("sheet", 0, type=int), len(sheets) - 1))
                page = max(1, min(request.args.get("page", 1, type=int), 1000))
                start = (page - 1) * 60 + 1
                sheet = book[sheets[sheet_number]]
                rows = [[str(c)[:1000] if c is not None else "" for c in row] for row in
                        sheet.iter_rows(min_row=start, max_row=min(start + 59, sheet.max_row or start + 59),
                                        max_col=min(sheet.max_column or 40, 40), values_only=True)] if not sheet.max_row or start <= sheet.max_row else []
                options.update(sheets=sheets, sheet=sheet_number, rows=rows, page=page, start=start,
                               more=(sheet.max_row or start) >= start + 60, wide=(sheet.max_column or 0) > 40)
        except Exception:
            options["preview_error"] = "工作簿无法读取，请检查它是否损坏或加密。"
        finally:
            if book:
                book.close()
    return options


@app.get("/files/<int:file_id>/preview")
def preview(file_id):
    record = public_file_or_404(file_id)
    if record["id"] != file_id:
        return redirect(url_for("preview", file_id=record["id"], **public_file_view_options(record, allow_embedded=True)))
    return render_template("preview.html", **file_preview(record))


ISSUE_FILTERS = {
    "all": ("Issues", "全部议题"),
    "created": ("Created by me", "我发起的"),
    "assigned": ("Assigned by me", "我指派的"),
    "mentioned": ("Mentioned", "提及我的"),
}
REACTIONS = {
    "+1": ("👍", "赞同"), "-1": ("👎", "不赞同"), "laugh": ("😄", "开心"),
    "confused": ("😕", "疑问"), "heart": ("❤️", "喜欢"), "hooray": ("🎉", "庆祝"),
    "rocket": ("🚀", "加油"), "eyes": ("👀", "关注"),
}


def issue_filter(value):
    return value if value in ISSUE_FILTERS else "all"


def issue_can_manage(issue):
    return g.user is not None and (is_admin() or issue["owner_id"] == g.user["id"])


def assign_issue(issue_id, assignee):
    if assignee is None:
        db().execute("DELETE FROM issue_assignments WHERE issue_id=?", (issue_id,))
        return
    db().execute("""INSERT INTO issue_assignments(issue_id,assignee_id,assigned_by) VALUES(?,?,?)
                    ON CONFLICT(issue_id) DO UPDATE SET assignee_id=excluded.assignee_id,assigned_by=excluded.assigned_by
                    WHERE issue_assignments.assignee_id<>excluded.assignee_id""", (issue_id, assignee, g.user["id"]))


def issue_redirect(issue_id, anchor=None):
    scope = issue_filter(request.form.get("scope"))
    return redirect(url_for("issues", issue=issue_id, scope=None if scope == "all" else scope, _anchor=anchor))


def active_issue(issue_id):
    return require("SELECT * FROM issues WHERE id=? AND deleted=0", (issue_id,))


def surviving_issue_thread(issue_id):
    row = one("""SELECT i.id FROM issues i WHERE i.id=? AND
                 (i.deleted=0 OR EXISTS (SELECT 1 FROM comments c WHERE c.issue_id=i.id AND c.deleted=0))""",
              (issue_id,))
    return row["id"] if row else None


@app.get("/issues")
def issues():
    scope = issue_filter(request.args.get("scope"))
    member_id = g.user["id"] if g.user else None
    conditions = {
        "all": ("1=1", ()),
        "created": ("i.owner_id=?", (member_id,)),
        "assigned": ("a.assigned_by=?", (member_id,)),
        "mentioned": ("""(i.mention_id=? OR EXISTS (
                     SELECT 1 FROM comments c JOIN comment_mentions m ON m.comment_id=c.id
                     WHERE c.issue_id=i.id AND c.deleted=0 AND m.member_id=?))""", (member_id, member_id)),
    }
    condition, args = conditions[scope]
    rows = query(f"""SELECT i.id,i.title,i.owner_id,i.resolution,i.created_at,i.deleted,
                     u.name author,ap.avatar author_avatar,p.name assignee,
                     a.assignee_id,(SELECT COUNT(*) FROM comments WHERE issue_id=i.id AND deleted=0) reply_count
                     FROM issues i JOIN users u ON u.id=i.owner_id
                     LEFT JOIN profiles ap ON ap.member_id=u.id
                     LEFT JOIN issue_assignments a ON a.issue_id=i.id LEFT JOIN users p ON p.id=a.assignee_id
                     WHERE ({condition}) AND (i.deleted=0 OR EXISTS (
                         SELECT 1 FROM comments c WHERE c.issue_id=i.id AND c.deleted=0))
                     ORDER BY i.id DESC""", args)
    issue_id = request.args.get("issue", type=int)
    current = require("""SELECT i.*,u.name author,ap.avatar author_avatar,m.name mentioned,t.title task_title,t.week,
                            a.assignee_id,a.assigned_by,p.name assignee,b.name assigner
                        FROM issues i JOIN users u ON u.id=i.owner_id
                        LEFT JOIN profiles ap ON ap.member_id=u.id
                        LEFT JOIN users m ON m.id=i.mention_id LEFT JOIN tasks t ON t.id=i.task_id
                        LEFT JOIN issue_assignments a ON a.issue_id=i.id
                        LEFT JOIN users p ON p.id=a.assignee_id LEFT JOIN users b ON b.id=a.assigned_by
                        WHERE i.id=?""", (issue_id,)) if issue_id else None
    comments, reaction_counts = [], {}
    if current:
        comments = query("""SELECT c.*,u.name author,ap.avatar author_avatar,mentioned.name mentioned,cm.member_id mention_id
                            FROM comments c
                            JOIN users u ON u.id=c.owner_id LEFT JOIN comment_mentions cm ON cm.comment_id=c.id
                            LEFT JOIN profiles ap ON ap.member_id=u.id
                            LEFT JOIN users mentioned ON mentioned.id=cm.member_id
                            WHERE c.issue_id=? AND c.deleted=0 ORDER BY c.id""", (issue_id,))
        if current["deleted"] and not comments:
            abort(404)
        totals = query("""SELECT 0 target,reaction,COUNT(*) total,MAX(member_id IS ?) selected
                          FROM issue_reactions WHERE issue_id=? GROUP BY reaction
                          UNION ALL
                          SELECT r.comment_id target,r.reaction,COUNT(*) total,MAX(r.member_id IS ?) selected
                          FROM comment_reactions r JOIN comments c ON c.id=r.comment_id
                          WHERE c.issue_id=? AND c.deleted=0 GROUP BY r.comment_id,r.reaction""",
                       (member_id, issue_id, member_id, issue_id))
        for row in totals:
            reaction_counts.setdefault(row["target"], {})[row["reaction"]] = row
    return render_template("issues.html", issues=rows, current=current, comments=comments, scope=scope,
                           issue_filters=ISSUE_FILTERS, reaction_types=REACTIONS, reaction_counts=reaction_counts,
                           can_manage=bool(current and not current["deleted"] and issue_can_manage(current)),
                           people=all_people() if g.user else (), tasks=get_tasks() if g.user else ())


@app.post("/issues")
@member
def create_issue():
    title, body = form("title"), form("body", 5000)
    task_id = form_id("task", False)
    if task_id:
        require("SELECT id FROM tasks WHERE id=?", (task_id,))
    mention, assignee = user_id("mention", False), user_id("assignee", False)
    with db():
        issue_id = db().execute("INSERT INTO issues(title,body,owner_id,mention_id,task_id) VALUES(?,?,?,?,?)",
                                (title, body, g.user["id"], mention, task_id)).lastrowid
        assign_issue(issue_id, assignee)
        audit("发起讨论", title, issue_id=issue_id)
    return redirect(url_for("issues", issue=issue_id))


@app.post("/issues/<int:issue_id>/assign")
@member
def change_issue_assignment(issue_id):
    with db():
        db().execute("BEGIN IMMEDIATE")
        issue = active_issue(issue_id)
        if not issue_can_manage(issue):
            abort(403)
        assignee = user_id("assignee", False)
        assign_issue(issue_id, assignee)
        audit("更改议题指派", issue["title"], issue_id=issue_id)
    return issue_redirect(issue_id)


@app.post("/issues/<int:issue_id>")
@member
def respond(issue_id):
    body = form("body", 5000)
    with db():
        db().execute("BEGIN IMMEDIATE")
        issue = active_issue(issue_id)
        if request.form.get("resolve"):
            if not issue_can_manage(issue):
                abort(403)
            db().execute("UPDATE issues SET resolution=? WHERE id=?", (body, issue_id))
            if issue["task_id"]:
                title = require("SELECT title FROM tasks WHERE id=?", (issue["task_id"],))["title"]
                audit("任务关联讨论已处理", f"{title} · {issue['title']}", issue_id=issue_id)
            return issue_redirect(issue_id)
        mention = user_id("mention", False)
        comment = db().execute("INSERT INTO comments(issue_id,owner_id,body) VALUES(?,?,?)",
                               (issue_id, g.user["id"], body)).lastrowid
        if mention is not None:
            db().execute("INSERT INTO comment_mentions(comment_id,member_id) VALUES(?,?)", (comment, mention))
    return issue_redirect(issue_id, f"comment-{comment}")


@app.post("/issues/<int:issue_id>/delete")
@member
def delete_issue(issue_id):
    with db():
        db().execute("BEGIN IMMEDIATE")
        issue = require("SELECT * FROM issues WHERE id=?", (issue_id,))
        if issue["owner_id"] != g.user["id"]:
            abort(403)
        if not issue["deleted"]:
            db().execute("""UPDATE issues SET deleted=1,title='',body='',resolution='',mention_id=NULL,task_id=NULL
                            WHERE id=?""", (issue_id,))
            assign_issue(issue_id, None)
            db().execute("DELETE FROM issue_reactions WHERE issue_id=?", (issue_id,))
            audit_label = f"议题 #{issue_id}"
            db().execute("UPDATE audit SET detail=? WHERE issue_id=?", (audit_label, issue_id))
            if not one("SELECT 1 FROM issues WHERE deleted=0 AND title=?", (issue["title"],)):
                # Old event records had no issue ID. Keep the event without exposing its old title.
                db().execute("""UPDATE audit SET detail=? WHERE issue_id IS NULL AND detail=?
                                AND event IN ('发起讨论','更改议题指派')""", (audit_label, issue["title"]))
                suffix = f" · {issue['title']}"
                db().execute("""UPDATE audit SET detail=?
                                WHERE issue_id IS NULL AND event='任务关联讨论已处理'
                                AND substr(detail,-length(?))=?
                                AND NOT EXISTS (
                                    SELECT 1 FROM issues i WHERE i.deleted=0 AND length(i.title)>length(?)
                                    AND substr(audit.detail,-length(' · ' || i.title))=' · ' || i.title
                                )""", (audit_label, suffix, suffix, issue["title"]))
            audit("删除议题", audit_label, issue_id=issue_id)
            flash("议题已删除。", "success")
        next_issue = surviving_issue_thread(issue_id)
    return issue_redirect(next_issue)


@app.post("/issues/<int:issue_id>/comments/<int:comment_id>/delete")
@member
def delete_comment(issue_id, comment_id):
    with db():
        db().execute("BEGIN IMMEDIATE")
        comment = require("SELECT owner_id,deleted FROM comments WHERE id=? AND issue_id=?", (comment_id, issue_id))
        if comment["owner_id"] != g.user["id"]:
            abort(403)
        if not comment["deleted"]:
            db().execute("UPDATE comments SET deleted=1,body='' WHERE id=?", (comment_id,))
            db().execute("DELETE FROM comment_mentions WHERE comment_id=?", (comment_id,))
            db().execute("DELETE FROM comment_reactions WHERE comment_id=?", (comment_id,))
            audit("删除评论", f"议题 #{issue_id} · 评论 #{comment_id}")
            flash("评论已删除。", "success")
        next_issue = surviving_issue_thread(issue_id)
    return issue_redirect(next_issue)


@app.post("/issues/<int:issue_id>/react")
@app.post("/issues/<int:issue_id>/comments/<int:comment_id>/react")
@member
def react_to_issue(issue_id, comment_id=None):
    reaction = form("reaction", 12)
    if reaction not in REACTIONS:
        abort(400, "请选择一个表情。")
    with db():
        db().execute("BEGIN IMMEDIATE")
        active_issue(issue_id)
        table, column, target = "issue_reactions", "issue_id", issue_id
        if comment_id is not None:
            require("SELECT id FROM comments WHERE id=? AND issue_id=? AND deleted=0", (comment_id, issue_id))
            table, column, target = "comment_reactions", "comment_id", comment_id
        values = target, g.user["id"], reaction
        deleted = db().execute(f"DELETE FROM {table} WHERE {column}=? AND member_id=? AND reaction=?", values)
        if not deleted.rowcount:
            db().execute(f"INSERT INTO {table}({column},member_id,reaction) VALUES(?,?,?)", values)
    return issue_redirect(issue_id, f"comment-{comment_id}" if comment_id is not None else "issue-body")


@app.get("/wiki")
def wiki():
    selected_tag = request.args.get("tag", "")[:35]
    rows = query("""SELECT l.*,r.stars,u.name author,p.avatar author_avatar
                   FROM links l JOIN users u ON u.id=l.owner_id
                   LEFT JOIN profiles p ON p.member_id=u.id
                   LEFT JOIN wiki_ratings r ON r.link_id=l.id
                   WHERE (?='' OR l.category=?) ORDER BY l.id DESC""", (selected_tag, selected_tag))
    tags = query("SELECT DISTINCT category FROM links ORDER BY category")
    return render_template("wiki.html", links=rows, tags=tags, selected_tag=selected_tag)


def wiki_url():
    value = form("url", 2000)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or chr(92) in value or "%" in parsed.netloc or parsed.port == 0):
            raise ValueError
        try:
            address = ip_address(host)
        except ValueError:
            labels = host.rstrip(".").encode("idna").decode("ascii").split(".")
            if (len(labels) < 2 or labels[-1].isdigit()
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                    or labels[-1] in ("localhost", "local", "internal", "invalid")):
                raise ValueError
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError
    except (ValueError, UnicodeError):
        abort(400, "请输入可公开访问的完整 HTTP 或 HTTPS 链接。")
    return value


@app.post("/wiki")
@app.post("/wiki/<int:link_id>/update")
@member
def save_wiki_link(link_id=None):
    title, url, tag = form("title"), wiki_url(), form("category", 35)
    rating = form_id("stars", False)
    stars = 3 if rating is None else rating
    note = form("note", 700, False)
    if not 1 <= stars <= 5:
        abort(400, "请选择 1 至 5 星。")
    with db():
        if link_id is None:
            link_id = db().execute("INSERT INTO links(title,url,category,note,owner_id) VALUES(?,?,?,?,?)",
                                   (title, url, tag, note, g.user["id"])).lastrowid
        else:
            current = require("SELECT owner_id FROM links WHERE id=?", (link_id,))
            if current["owner_id"] != g.user["id"]:
                abort(403)
            db().execute("UPDATE links SET title=?,url=?,category=?,note=? WHERE id=?", (title, url, tag, note, link_id))
        db().execute("""INSERT INTO wiki_ratings(link_id,stars) VALUES(?,?)
                        ON CONFLICT(link_id) DO UPDATE SET stars=excluded.stars""", (link_id, stars))
    return redirect(url_for("wiki", _anchor=f"link-{link_id}"))




MEMBER_ACTIVITY_COLUMNS = """
    (SELECT count(*) FROM files WHERE owner_id=u.id AND withdrawn=0) AS uploads,
    (SELECT count(*) FROM issues WHERE owner_id=u.id AND deleted=0) +
    (SELECT count(*) FROM comments WHERE owner_id=u.id AND deleted=0) AS discussions
"""


@app.get("/people")
def people():
    people = query(f"""SELECT u.id,u.name,u.role,u.group_id,g.title group_name,g.chapters,p.avatar,p.bio,
                    {MEMBER_ACTIVITY_COLUMNS}
                    FROM users u LEFT JOIN groups g ON g.id=u.group_id LEFT JOIN profiles p ON p.member_id=u.id
                    LEFT JOIN roster r ON r.member_id=u.id
                    ORDER BY r.seat IS NULL,r.seat,u.initials COLLATE NOCASE,u.name""")
    assignments = {p["id"]: [] for p in people}
    for row in query("SELECT id,title,week,owner_id FROM tasks ORDER BY week,id"):
        assignments.setdefault(row["owner_id"], []).append(row)
    return render_template("people.html", people=people, assignments=assignments, groups=query("SELECT * FROM groups"))


@app.get("/people/<int:person_id>")
def person(person_id):
    current = require(f"""SELECT u.id,u.name,u.role,u.group_id,p.bio,p.avatar,r.seat,
                         {MEMBER_ACTIVITY_COLUMNS}
                         FROM users u LEFT JOIN profiles p ON p.member_id=u.id
                         LEFT JOIN roster r ON r.member_id=u.id WHERE u.id=?""", (person_id,))
    contributions = query(LATEST_FILES + """ SELECT c.detail,c.file_id,c.task_id,t.week,
                             f.id latest_file_id,f.group_id latest_file_group_id,f.name file_name FROM contributions c
                             JOIN tasks t ON t.id=c.task_id AND t.status='done'
                             LEFT JOIN latest_files ON latest_files.id=c.file_id
                             LEFT JOIN files f ON f.id=latest_files.latest_id AND f.withdrawn=0
                             WHERE c.member_id=? AND c.kind='deliverable' AND c.status='approved'
                             ORDER BY c.id DESC""", (person_id,))
    return render_template("person.html", person=current,
                           tasks=[t for t in get_tasks() if t["owner_id"] == person_id], contributions=contributions)


@app.post("/people/create")
@member
def create_person():
    if not is_admin():
        abort(403)
    try:
        fields = account_fields(form("username", 40).lower(), form("name", 65), form("password", 256),
                                form("role", 12), form_id("group", False), form("initials", 20, False))
        if fields[3] == "editor" or (fields[3] == "hr" and g.user["role"] != "editor"):
            abort(403)
        with db():
            db().execute("BEGIN IMMEDIATE")
            if one("SELECT COUNT(*) AS n FROM users")["n"] >= 30:
                abort(409, "当前项目设有 30 个成员席位。")
            member_id = db().execute("INSERT INTO users(username,name,password,role,group_id,initials) VALUES(?,?,?,?,?,?)", fields).lastrowid
            audit("建立账号", fields[1])
    except (sqlite3.IntegrityError, ValueError) as exc:
        flash("无法建立账号，请检查账号是否重复、岗位是否已有成员，以及小组和密码格式。", "error")
        return redirect(url_for("people"))
    return redirect(url_for("person", person_id=member_id))


@app.post("/people/<int:person_id>/reset")
@member
def reset_person(person_id):
    current = require("SELECT * FROM users WHERE id=?", (person_id,))
    if not is_admin() or current["role"] == "editor" or (current["role"] == "hr" and g.user["role"] != "editor"):
        abort(403)
    temporary = form("password", 256)
    if len(temporary) < 12:
        abort(400, "临时密码至少 12 字符。")
    with db():
        db().execute("UPDATE users SET password=?, must_change=1, auth_version=auth_version+1 WHERE id=?",
                     (generate_password_hash(temporary), person_id))
        audit("重置成员密码", current["name"])
    flash("已重置密码，下次登录时须改密。", "success")
    return redirect(url_for("person", person_id=person_id))


@app.post("/people/profile")
@member
def update_profile():
    own_id = g.user["id"]
    bio = form("bio", 1200, False) if "bio" in request.form else None
    uploaded = request.files.get("avatar")
    if bio is None and not uploaded:
        abort(400, "请填写个人感想或选择头像。")
    avatar = None
    if uploaded:
        raw = uploaded.stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            abort(400, "头像上限为 2 MB。")
        try:
            with Image.open(io.BytesIO(raw), formats=("JPEG", "PNG", "WEBP")) as source:
                if source.width * source.height > 6_000_000:
                    abort(400, "头像图片的分辨率过大。")
                thumbnail = ImageOps.exif_transpose(source).convert("RGB")
                thumbnail.thumbnail((320, 320))
                avatar = f"{uuid4().hex}.jpg"
                thumbnail.save(AVATARS / avatar, format="JPEG", quality=88, optimize=True)
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            abort(400, "请选择可读取的 JPEG、PNG 或 WebP 图片。")
    try:
        with db():
            db().execute("""INSERT INTO profiles(member_id,bio,avatar) VALUES(?,COALESCE(?,''),COALESCE(?,''))
                         ON CONFLICT(member_id) DO UPDATE
                         SET bio=COALESCE(?,profiles.bio),avatar=COALESCE(?,profiles.avatar)""",
                         (own_id, bio, avatar, bio, avatar))
    except BaseException:
        if avatar:
            (AVATARS / avatar).unlink(missing_ok=True)
        raise
    flash("个人信息已保存。", "success")
    return redirect(url_for("person", person_id=own_id))


@app.get("/people/<int:person_id>/avatar")
def avatar(person_id):
    record = require("SELECT avatar FROM profiles WHERE member_id=?", (person_id,))
    if not record["avatar"] or (not g.user and not PUBLIC_ROSTER) or not (AVATARS / record["avatar"]).is_file():
        abort(404)
    return send_file(AVATARS / record["avatar"], mimetype="image/jpeg")


@app.get("/activity")
def activity():
    project_events = ("新建任务", "更新任务", "任务进度", "上传文件", "更新文件版本",
                      "公开文件", "取消公开", "撤下文件", "新建文件夹", "重命名文件夹", "移动文件",
                      "发起讨论", "更改议题指派", "任务关联讨论已处理", "删除议题", "删除评论")
    placeholders = ",".join("?" for _ in project_events)
    where = "" if is_admin() else f"WHERE event IN ({placeholders})"
    records = query(f"SELECT actor,event,detail,created_at FROM audit {where} ORDER BY id DESC LIMIT 200",
                    () if is_admin() else project_events)
    return render_template("activity.html", records=records)
