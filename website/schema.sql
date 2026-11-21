PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, title TEXT NOT NULL, chapters TEXT NOT NULL);
INSERT OR IGNORE INTO groups VALUES
 (1,'第 1 组','规划 · 岗位'),(2,'第 2 组','招聘'),(3,'第 3 组','培训'),
 (4,'第 4 组','绩效'),(5,'第 5 组','薪酬'),(6,'第 6 组','关系');
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 nickname TEXT NOT NULL DEFAULT '', password TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('editor','hr','lead','liaison','member')),
 group_id INTEGER REFERENCES groups(id), must_change INTEGER NOT NULL DEFAULT 1,
 auth_version INTEGER NOT NULL DEFAULT 0, initials TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_lead_per_group ON users(group_id) WHERE role='lead';
CREATE UNIQUE INDEX IF NOT EXISTS one_per_admin_role ON users(role) WHERE role IN ('editor','hr');
CREATE TABLE IF NOT EXISTS file_folders(
 id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES groups(id),
 parent_id INTEGER REFERENCES file_folders(id), name TEXT NOT NULL, name_key TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_sibling_file_folder
 ON file_folders(COALESCE(group_id,0),COALESCE(parent_id,0),name_key);
CREATE TABLE IF NOT EXISTS tasks(
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, week INTEGER NOT NULL CHECK(week BETWEEN 13 AND 18),
 group_id INTEGER REFERENCES groups(id), parent_id INTEGER REFERENCES tasks(id),
 owner_id INTEGER NOT NULL REFERENCES users(id), reviewer_id INTEGER NOT NULL REFERENCES users(id),
 collaborator_id INTEGER REFERENCES users(id), due TEXT NOT NULL, deliverable TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'todo', risk TEXT NOT NULL DEFAULT 'green',
 file_id INTEGER REFERENCES files(id), waiting_for INTEGER REFERENCES users(id),
 blocker TEXT NOT NULL DEFAULT '', reply_due TEXT NOT NULL DEFAULT '',
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS files(
 id INTEGER PRIMARY KEY, previous_id INTEGER UNIQUE REFERENCES files(id),
 name TEXT NOT NULL, blob TEXT NOT NULL UNIQUE, ext TEXT NOT NULL, size INTEGER NOT NULL,
 group_id INTEGER REFERENCES groups(id), owner_id INTEGER NOT NULL REFERENCES users(id),
 folder_id INTEGER REFERENCES file_folders(id),
 public INTEGER NOT NULL DEFAULT 0, withdrawn INTEGER NOT NULL DEFAULT 0,
 preview_status TEXT NOT NULL DEFAULT 'ready', uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS issues(
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
 owner_id INTEGER NOT NULL REFERENCES users(id), mention_id INTEGER REFERENCES users(id),
 task_id INTEGER REFERENCES tasks(id), resolution TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1))
);
CREATE TABLE IF NOT EXISTS comments(
 id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id), owner_id INTEGER NOT NULL REFERENCES users(id),
 body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1))
);
CREATE TABLE IF NOT EXISTS links(
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
 category TEXT NOT NULL, note TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS issue_assignments(
 issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
 assignee_id INTEGER NOT NULL REFERENCES users(id),
 assigned_by INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS comment_mentions(
 comment_id INTEGER PRIMARY KEY REFERENCES comments(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS issue_reactions(
 issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES users(id),
 reaction TEXT NOT NULL,
 PRIMARY KEY(issue_id, member_id, reaction)
);
CREATE TABLE IF NOT EXISTS comment_reactions(
 comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES users(id),
 reaction TEXT NOT NULL,
 PRIMARY KEY(comment_id, member_id, reaction)
);
CREATE TABLE IF NOT EXISTS wiki_ratings(
 link_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE,
 stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5)
);
CREATE INDEX IF NOT EXISTS issue_comment_threads ON comments(issue_id, id);
CREATE INDEX IF NOT EXISTS issue_assignment_authors ON issue_assignments(assigned_by);
CREATE TABLE IF NOT EXISTS contributions(
 id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL, detail TEXT NOT NULL, evidence TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', file_id INTEGER REFERENCES files(id), task_id INTEGER REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS login_limits(
 key TEXT PRIMARY KEY, failures INTEGER NOT NULL, until INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles(
 member_id INTEGER PRIMARY KEY REFERENCES users(id),
 bio TEXT NOT NULL DEFAULT '',
 avatar TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS roster(
 seat INTEGER PRIMARY KEY CHECK(seat BETWEEN 1 AND 30),
 member_id INTEGER NOT NULL UNIQUE REFERENCES users(id)
);
