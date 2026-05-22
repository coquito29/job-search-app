"""
blueprints/applications.py  —  Applications tracker CRUD.

Routes:
  GET    /api/applications        list (optional ?status=Applied filter)
  POST   /api/applications        create
  PATCH  /api/applications/<id>   update
  DELETE /api/applications/<id>   delete
  GET    /api/applications/urls   dedupe index (url + company|title fingerprints)
  GET    /api/applications/stats  status counts

All routes require auth (single-user passcode model from db._auth_required).
"""
from datetime import datetime

from flask import Blueprint, jsonify, request

from db import _auth_required, _db_conn, _row_get, APP_STATUSES


bp = Blueprint("applications", __name__)


@bp.route("/api/applications", methods=["GET"])
def list_applications():
    uid, err = _auth_required()
    if err: return err
    status_filter = request.args.get("status")
    with _db_conn() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM applications WHERE user_id = ? AND status = ? ORDER BY applied_at DESC",
                (uid, status_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM applications WHERE user_id = ? ORDER BY applied_at DESC",
                (uid,),
            ).fetchall()
    return jsonify({"applications": [dict(r) for r in rows], "count": len(rows)})


@bp.route("/api/applications", methods=["POST"])
def create_application():
    uid, err = _auth_required()
    if err: return err
    data = request.get_json(force=True) or {}
    company = (data.get("company") or "").strip()
    title   = (data.get("title") or "").strip()
    if not company or not title:
        return jsonify({"error": "company and title are required"}), 400
    status = (data.get("status") or "Applied").strip()
    if status not in APP_STATUSES:
        return jsonify({"error": f"status must be one of {APP_STATUSES}"}), 400
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    # RETURNING id works on both sqlite (>=3.35) and Postgres — keeps the
    # adapter backend-agnostic (lastrowid is sqlite-only)
    with _db_conn() as conn:
        cur = conn.execute(
            """INSERT INTO applications
               (user_id, company, title, url, ats, location, salary, source, status, notes, applied_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                uid, company, title,
                (data.get("url") or "").strip() or None,
                (data.get("ats") or "").strip() or None,
                (data.get("location") or "").strip() or None,
                (data.get("salary") or "").strip() or None,
                (data.get("source") or "").strip() or None,
                status,
                (data.get("notes") or "").strip() or None,
                now, now,
            ),
        )
        row = cur.fetchone()
        new_id = row["id"] if row is not None else None
    return jsonify({"id": new_id, "ok": True})


@bp.route("/api/applications/<int:app_id>", methods=["PATCH"])
def update_application(app_id):
    uid, err = _auth_required()
    if err: return err
    data = request.get_json(force=True) or {}
    fields = []
    values = []
    for f in ("company", "title", "url", "ats", "location", "salary", "source", "status", "notes"):
        if f in data:
            if f == "status" and data[f] not in APP_STATUSES:
                return jsonify({"error": f"status must be one of {APP_STATUSES}"}), 400
            fields.append(f"{f} = ?")
            values.append((data[f] or "").strip() or None if f != "status" else data[f])
    if not fields:
        return jsonify({"error": "No fields to update"}), 400
    fields.append("updated_at = ?")
    values.append(datetime.utcnow().isoformat(timespec="seconds") + "Z")
    values.append(app_id)
    values.append(uid)
    with _db_conn() as conn:
        cur = conn.execute(
            f"UPDATE applications SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            values,
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@bp.route("/api/applications/<int:app_id>", methods=["DELETE"])
def delete_application(app_id):
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM applications WHERE id = ? AND user_id = ?", (app_id, uid)
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@bp.route("/api/applications/urls", methods=["GET"])
def applied_urls():
    """Lightweight index of already-applied jobs — used by the frontend
    to dedupe jobs you've already submitted (by URL or company|title).
    Returns:
      { urls: { <url>: {applied_at, status, company, title} },
        fingerprints: { "<co>|<title>": {applied_at, status} } }
    """
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, company, title, url, status, applied_at, notes "
            "FROM applications WHERE user_id = ?",
            (uid,),
        ).fetchall()
    urls = {}
    fingerprints = {}
    for r in rows:
        meta = {
            "id":         r["id"],
            "applied_at": r["applied_at"],
            "status":     r["status"],
            "company":    r["company"],
            "title":      r["title"],
            "notes":      r["notes"] or "",
        }
        if r["url"]:
            urls[r["url"]] = meta
        co = (r["company"] or "").lower().strip()
        ti = (r["title"]   or "").lower().strip()
        if co and ti:
            fingerprints[f"{co}|{ti}"] = {
                "id":         r["id"],
                "applied_at": r["applied_at"],
                "status":     r["status"],
                "notes":      r["notes"] or "",
            }
    return jsonify({"urls": urls, "fingerprints": fingerprints})


@bp.route("/api/applications/stats", methods=["GET"])
def application_stats():
    uid, err = _auth_required()
    if err: return err
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications WHERE user_id = ? GROUP BY status",
            (uid,),
        ).fetchall()
        total = _row_get(
            conn.execute(
                "SELECT COUNT(*) AS n FROM applications WHERE user_id = ?", (uid,)
            ).fetchone(),
            "n", 0,
        )
    by_status = {s: 0 for s in APP_STATUSES}
    for r in rows:
        by_status[r["status"]] = r["n"]
    return jsonify({"total": total, "by_status": by_status})
