"""Projects, to-do list, progress tracking and long-term memory (all local, in SQLite).

Every public function here is exposed to Gemini as a tool, so docstrings are written for the model.
"""
from __future__ import annotations

from typing import Optional

from . import db

PRIORITIES = ("low", "medium", "high")
_PRIO_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"


def _resolve_project(project: Optional[str]) -> Optional[dict]:
    if project is None or str(project).strip() == "":
        return None
    p = str(project).strip()
    if p.isdigit():
        row = db.query_one("SELECT * FROM projects WHERE id=?", (int(p),))
        if row:
            return row
    row = db.query_one("SELECT * FROM projects WHERE name=?", (p,))
    if row:
        return row
    rows = db.query("SELECT * FROM projects WHERE name LIKE ? ORDER BY status='done', id", (f"%{p}%",))
    return rows[0] if rows else None


def _need_project(project: str) -> dict:
    row = _resolve_project(project)
    if not row:
        names = [r["name"] for r in db.query("SELECT name FROM projects ORDER BY id")]
        raise ValueError(f"No project matching '{project}'. Existing projects: {names or 'none'}")
    return row


def _progress(project_id: int) -> dict:
    r = db.query_one(
        "SELECT COUNT(*) total, SUM(status='done') done, SUM(status='in_progress') doing "
        "FROM tasks WHERE project_id=?",
        (project_id,),
    ) or {}
    total, done = r.get("total") or 0, r.get("done") or 0
    return {
        "tasks_total": total,
        "tasks_done": done,
        "tasks_in_progress": r.get("doing") or 0,
        "progress_percent": round(100 * done / total) if total else 0,
    }


def _norm_prio(priority: Optional[str]) -> str:
    p = (priority or "medium").lower().strip()
    return p if p in PRIORITIES else "medium"


# ── projects ──────────────────────────────────────────────────
def list_projects(include_done: bool = False) -> list[dict]:
    """List the user's projects with progress percentage, open task counts and deadlines.

    Args:
        include_done: Also include completed projects.
    """
    sql = "SELECT * FROM projects"
    if not include_done:
        sql += " WHERE status != 'done'"
    sql += f" ORDER BY status='paused', {_PRIO_ORDER}, deadline IS NULL, deadline"
    out = []
    for p in db.query(sql):
        p.update(_progress(p["id"]))
        last = db.query_one(
            "SELECT note, created_at FROM progress_log WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],)
        )
        p["last_update"] = last
        out.append(p)
    return out


def get_project(project: str) -> dict:
    """Get full details of one project: description, all tasks and the recent progress log.

    Args:
        project: Project name (partial match is fine) or numeric id.
    """
    p = _need_project(project)
    p.update(_progress(p["id"]))
    p["tasks"] = db.query(
        f"SELECT * FROM tasks WHERE project_id=? ORDER BY status='done', {_PRIO_ORDER}, due_date IS NULL, due_date",
        (p["id"],),
    )
    p["progress_log"] = db.query(
        "SELECT note, created_at FROM progress_log WHERE project_id=? ORDER BY id DESC LIMIT 15", (p["id"],)
    )
    return p


def create_project(
    name: str,
    description: str = "",
    priority: str = "medium",
    deadline: Optional[str] = None,
) -> dict:
    """Create a new project to track.

    Args:
        name: Short project name.
        description: What the project is about and its goal.
        priority: low, medium or high.
        deadline: Optional deadline as YYYY-MM-DD.
    """
    ts = db.now_iso()
    pid = db.execute(
        "INSERT INTO projects(name, description, priority, deadline, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (name.strip(), description, _norm_prio(priority), deadline, ts, ts),
    )
    return get_project(str(pid))


def update_project(
    project: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    deadline: Optional[str] = None,
) -> dict:
    """Update a project's name, description, status (active, paused, done), priority or deadline.

    Args:
        project: Project name or id.
        new_name: New name.
        description: New description.
        status: active, paused or done.
        priority: low, medium or high.
        deadline: YYYY-MM-DD, or "none" to clear.
    """
    p = _need_project(project)
    fields: dict = {}
    if new_name:
        fields["name"] = new_name.strip()
    if description is not None:
        fields["description"] = description
    if status:
        s = status.lower().strip()
        if s not in ("active", "paused", "done"):
            raise ValueError("status must be active, paused or done")
        fields["status"] = s
    if priority:
        fields["priority"] = _norm_prio(priority)
    if deadline is not None:
        fields["deadline"] = None if deadline.lower() in ("none", "") else deadline
    if fields:
        fields["updated_at"] = db.now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), p["id"]))
    return get_project(str(p["id"]))


def delete_project(project: str) -> dict:
    """Permanently delete a project and its progress log (its tasks become unassigned).
    Only do this when the user explicitly asks to delete.

    Args:
        project: Project name or id.
    """
    p = _need_project(project)
    db.execute("DELETE FROM projects WHERE id=?", (p["id"],))
    return {"deleted": p["name"]}


def log_progress(project: str, note: str) -> dict:
    """Record a progress update / milestone / note for a project (e.g. "finished the landing page").

    Args:
        project: Project name or id.
        note: What happened.
    """
    p = _need_project(project)
    db.execute(
        "INSERT INTO progress_log(project_id, note, created_at) VALUES(?,?,?)", (p["id"], note, db.now_iso())
    )
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (db.now_iso(), p["id"]))
    return {"logged": note, "project": p["name"], **_progress(p["id"])}


# ── tasks ─────────────────────────────────────────────────────
def add_task(
    title: str,
    project: Optional[str] = None,
    due_date: Optional[str] = None,
    priority: str = "medium",
    estimate_minutes: Optional[int] = None,
    notes: str = "",
) -> dict:
    """Add a to-do item, optionally attached to a project.

    Args:
        title: What needs doing.
        project: Project name or id to attach it to (optional).
        due_date: Optional due date YYYY-MM-DD.
        priority: low, medium or high.
        estimate_minutes: Rough time estimate in minutes (helps schedule planning).
        notes: Extra details.
    """
    pid = _need_project(project)["id"] if project else None
    tid = db.execute(
        "INSERT INTO tasks(project_id, title, notes, priority, due_date, estimate_minutes, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (pid, title.strip(), notes, _norm_prio(priority), due_date, estimate_minutes, db.now_iso()),
    )
    return _task(tid)


def _task(task_id: int) -> dict:
    t = db.query_one(
        "SELECT t.*, p.name AS project FROM tasks t LEFT JOIN projects p ON p.id=t.project_id WHERE t.id=?",
        (task_id,),
    )
    if not t:
        raise ValueError(f"No task with id {task_id}")
    return t


def list_tasks(
    project: Optional[str] = None,
    status: str = "open",
    due_before: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """List to-do items, sorted by priority and due date.

    Args:
        project: Filter by project name or id.
        status: "open" (todo + in_progress), "todo", "in_progress", "done" or "all".
        due_before: Only tasks due on or before this date (YYYY-MM-DD).
        limit: Maximum number of tasks.
    """
    where, params = [], []
    if project:
        where.append("t.project_id=?")
        params.append(_need_project(project)["id"])
    s = (status or "open").lower()
    if s == "open":
        where.append("t.status != 'done'")
    elif s in ("todo", "in_progress", "done"):
        where.append("t.status=?")
        params.append(s)
    if due_before:
        where.append("t.due_date IS NOT NULL AND t.due_date <= ?")
        params.append(due_before)
    sql = "SELECT t.*, p.name AS project FROM tasks t LEFT JOIN projects p ON p.id=t.project_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = "t.completed_at DESC" if s == "done" else (
        "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.due_date IS NULL, t.due_date, t.id"
    )
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(int(limit))
    return db.query(sql, params)


def update_task(
    task_id: int,
    title: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    estimate_minutes: Optional[int] = None,
    notes: Optional[str] = None,
    project: Optional[str] = None,
) -> dict:
    """Edit a task. Use status "in_progress" when the user starts on it and "done" when finished.

    Args:
        task_id: Task id.
        title: New title.
        status: todo, in_progress or done.
        priority: low, medium or high.
        due_date: YYYY-MM-DD or "none" to clear.
        estimate_minutes: New estimate.
        notes: New notes.
        project: Move to this project (name or id), or "none" to detach.
    """
    _task(task_id)
    fields: dict = {}
    if title:
        fields["title"] = title
    if status:
        s = status.lower().strip()
        if s not in ("todo", "in_progress", "done"):
            raise ValueError("status must be todo, in_progress or done")
        fields["status"] = s
        fields["completed_at"] = db.now_iso() if s == "done" else None
    if priority:
        fields["priority"] = _norm_prio(priority)
    if due_date is not None:
        fields["due_date"] = None if due_date.lower() in ("none", "") else due_date
    if estimate_minutes is not None:
        fields["estimate_minutes"] = estimate_minutes
    if notes is not None:
        fields["notes"] = notes
    if project is not None:
        fields["project_id"] = None if project.lower() in ("none", "") else _need_project(project)["id"]
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
    return _task(task_id)


def complete_task(task_id: int) -> dict:
    """Mark a task as done.

    Args:
        task_id: Task id.
    """
    t = update_task(task_id, status="done")
    if t.get("project_id"):
        t["project_progress"] = _progress(t["project_id"])
    return t


def delete_task(task_id: int) -> dict:
    """Delete a task permanently.

    Args:
        task_id: Task id.
    """
    t = _task(task_id)
    db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"deleted": t["title"]}


# ── memory ────────────────────────────────────────────────────
def remember(fact: str) -> dict:
    """Store a long-term fact about the user (preferences, people, routines, context) so you remember it
    in every future conversation. Use this whenever the user tells you something worth remembering.

    Args:
        fact: The fact, written as a full sentence.
    """
    mid = db.execute("INSERT INTO memories(fact, created_at) VALUES(?,?)", (fact.strip(), db.now_iso()))
    return {"id": mid, "remembered": fact}


def list_memories() -> list[dict]:
    """List everything you have been asked to remember about the user (with ids)."""
    return db.query("SELECT id, fact, created_at FROM memories ORDER BY id")


def forget(memory_id: int) -> dict:
    """Delete a stored memory by id.

    Args:
        memory_id: The memory id from list_memories.
    """
    db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return {"forgotten": memory_id}
