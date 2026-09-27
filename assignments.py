"""分配事务：评审时段、忙碌区间、邀请/响应/评审提交与主席补位视图。

以混入类形式供 ReviewStore 继承；宿主需提供 connect()、_user()、_require()、
_audit()。时段的纯规则（日期校验、区间重叠、整周期覆盖）见 availability.py。
"""
from __future__ import annotations

import sqlite3

from availability import overlaps, utcnow, validate_period
from errors import BusinessError

REQUIRED_REVIEWS = 2  # 每篇论文所需评审数，与 decide 的“至少两份已完成评审”一致。


class AssignmentOps:
    # ---------- 评审时段（主席维护） ----------

    def set_review_period(self, chair_id: str, paper_id: int, start: str, end: str) -> dict:
        start, end = validate_period(start, end)
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或当前不可设定评审时段", 409, "paper_unavailable")
                conn.execute(
                    "UPDATE papers SET review_start=?, review_end=? WHERE id=?",
                    (start, end, paper_id),
                )
                self._audit(conn, paper_id, chair_id, "paper.review_period", {"review_start": start, "review_end": end})
                # 时段变化后，不再覆盖整个周期的进行中分配同样释放。
                released = self._release_unavailable(conn, paper_id, chair_id)
                return {
                    "paper_id": paper_id,
                    "review_start": start,
                    "review_end": end,
                    "released_assignment_ids": released,
                }
            except Exception:
                conn.rollback()
                raise

    # ---------- 忙碌区间（评审人维护） ----------

    def add_busy_interval(self, reviewer_id: str, start: str, end: str, reason: str = "") -> dict:
        start, end = validate_period(start, end)
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO busy_intervals(reviewer_id,start_date,end_date,reason,created_at) VALUES(?,?,?,?,?)",
                    (reviewer_id, start, end, reason.strip(), utcnow()),
                )
                interval_id = cur.lastrowid
                self._audit(conn, None, reviewer_id, "busy_interval.add", {"interval_id": interval_id, "start": start, "end": end})
                released = []
                rows = conn.execute(
                    """SELECT a.id AS assignment_id, a.paper_id, p.review_start, p.review_end
                       FROM assignments a JOIN papers p ON p.id = a.paper_id
                       WHERE a.reviewer_id=? AND a.status IN ('invited','accepted') AND a.released_at IS NULL
                             AND p.review_start IS NOT NULL AND p.review_end IS NOT NULL""",
                    (reviewer_id,),
                ).fetchall()
                for row in rows:
                    if overlaps(start, end, row["review_start"], row["review_end"]):
                        self._release(conn, row["assignment_id"], row["paper_id"], reviewer_id,
                                      {"cause": "busy_interval", "busy_interval_id": interval_id})
                        released.append(row["assignment_id"])
                return {
                    "id": interval_id,
                    "reviewer_id": reviewer_id,
                    "start_date": start,
                    "end_date": end,
                    "reason": reason.strip(),
                    "released_assignment_ids": released,
                }
            except Exception:
                conn.rollback()
                raise

    def list_busy_intervals(self, user_id: str, reviewer_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "reviewer":
                reviewer_id = user_id  # 评审人只能看自己的区间。
            elif user["role"] != "chair":
                raise BusinessError("该操作仅允许 reviewer 或 chair 角色", 403, "forbidden")
            if reviewer_id:
                rows = conn.execute(
                    "SELECT * FROM busy_intervals WHERE reviewer_id=? ORDER BY start_date, id", (reviewer_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM busy_intervals ORDER BY reviewer_id, start_date, id").fetchall()
            return [dict(row) for row in rows]

    def delete_busy_interval(self, reviewer_id: str, interval_id: int) -> dict:
        # 删除区间不会自动恢复已释放的分配，需主席重新邀请。
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            cur = conn.execute(
                "DELETE FROM busy_intervals WHERE id=? AND reviewer_id=?", (interval_id, reviewer_id)
            )
            if cur.rowcount == 0:
                raise BusinessError("忙碌区间不存在或不属于当前评审人", 404, "not_found")
            self._audit(conn, None, reviewer_id, "busy_interval.delete", {"interval_id": interval_id})
            return {"deleted": interval_id}

    # ---------- 分配事务 ----------

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                    raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
                if not paper["review_start"] or not paper["review_end"]:
                    raise BusinessError("主席尚未为该论文设定评审时段", 409, "review_period_missing")
                busy = conn.execute(
                    """SELECT start_date, end_date FROM busy_intervals
                       WHERE reviewer_id=? AND start_date<=? AND end_date>=? ORDER BY id LIMIT 1""",
                    (reviewer_id, paper["review_end"], paper["review_start"]),
                ).fetchone()
                if busy:
                    raise BusinessError(
                        f"评审人在 {busy['start_date']}~{busy['end_date']} 登记了忙碌，无法覆盖整个评审时段",
                        409, "reviewer_unavailable")
                load = self._active_load(conn, reviewer_id)
                if load >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                existing = conn.execute(
                    "SELECT * FROM assignments WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id)
                ).fetchone()
                if existing and existing["released_at"] is None:
                    raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                if existing:  # 已释放的分配允许重新邀请，复用原行以保留历史。
                    conn.execute(
                        "UPDATE assignments SET status='invited', released_at=NULL, score=NULL, review_text=NULL, updated_at=? WHERE id=?",
                        (utcnow(), existing["id"]),
                    )
                    assignment_id = existing["id"]
                else:
                    try:
                        cur = conn.execute(
                            "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
                            (paper_id, reviewer_id, utcnow(), utcnow()),
                        )
                    except sqlite3.IntegrityError:
                        raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                    assignment_id = cur.lastrowid
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                self._audit(conn, paper_id, chair_id, "assignment.invite", {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
                return {"id": assignment_id, "paper_id": paper_id, "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["released_at"] is not None:
                raise BusinessError("该分配已因休假冲突被释放", 409, "assignment_released")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            status = "accepted" if accepted else "declined"
            conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
            self._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {"assignment_id": assignment_id, "status": status})
            return {"id": assignment_id, "status": status}

    def submit_review(self, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        if len(text.strip()) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["released_at"] is not None:
                raise BusinessError("该分配已因休假冲突被释放", 409, "assignment_released")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
            conn.execute(
                "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                (score, text.strip(), utcnow(), assignment_id),
            )
            self._audit(conn, row["paper_id"], reviewer_id, "review.submit", {"assignment_id": assignment_id, "score": score})
            return {"id": assignment_id, "status": "completed", "score": score}

    # ---------- 主席补位视图 ----------

    def backfill_overview(self, chair_id: str) -> dict:
        """待补位论文列表：每篇论文的缺口、以及每个候选评审人被排除的原因。"""
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            papers = conn.execute(
                "SELECT * FROM papers WHERE status IN ('submitted','under_review') ORDER BY id"
            ).fetchall()
            reviewers = conn.execute("SELECT * FROM users WHERE role='reviewer' ORDER BY id").fetchall()
            items = []
            for paper in papers:
                stats = conn.execute(
                    """SELECT
                         COALESCE(SUM(CASE WHEN status IN ('invited','accepted') AND released_at IS NULL THEN 1 ELSE 0 END), 0) AS active,
                         COALESCE(SUM(CASE WHEN status='completed' AND released_at IS NULL THEN 1 ELSE 0 END), 0) AS completed,
                         COALESCE(SUM(CASE WHEN released_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS released
                       FROM assignments WHERE paper_id=?""",
                    (paper["id"],),
                ).fetchone()
                shortage = max(0, REQUIRED_REVIEWS - stats["active"] - stats["completed"])
                items.append({
                    "paper_id": paper["id"],
                    "title": paper["title"],
                    "status": paper["status"],
                    "review_start": paper["review_start"],
                    "review_end": paper["review_end"],
                    "active": stats["active"],
                    "completed": stats["completed"],
                    "released": stats["released"],
                    "required": REQUIRED_REVIEWS,
                    "shortage": shortage,
                    "needs_backfill": shortage > 0,
                    "candidates": [self._candidate_view(conn, paper, r) for r in reviewers],
                })
            items.sort(key=lambda item: (not item["needs_backfill"], item["paper_id"]))
            return {"required_reviews": REQUIRED_REVIEWS, "items": items}

    # ---------- 内部辅助 ----------

    @staticmethod
    def _active_load(conn: sqlite3.Connection, reviewer_id: str) -> int:
        """进行中（invited/accepted 且未释放）的分配数，即当前占用的负载名额。"""
        return conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted') AND released_at IS NULL",
            (reviewer_id,),
        ).fetchone()[0]

    def _release(self, conn: sqlite3.Connection, assignment_id: int, paper_id: int, actor_id: str, detail: dict) -> None:
        conn.execute("UPDATE assignments SET released_at=?, updated_at=? WHERE id=?", (utcnow(), utcnow(), assignment_id))
        self._audit(conn, paper_id, actor_id, "assignment.release", {"assignment_id": assignment_id, **detail})

    def _release_unavailable(self, conn: sqlite3.Connection, paper_id: int, actor_id: str) -> list[int]:
        """释放评审时段与忙碌区间冲突的进行中分配；已完成评审与决定不受影响。"""
        paper = conn.execute("SELECT review_start, review_end FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not paper or not paper["review_start"] or not paper["review_end"]:
            return []
        rows = conn.execute(
            """SELECT id, reviewer_id FROM assignments
               WHERE paper_id=? AND status IN ('invited','accepted') AND released_at IS NULL""",
            (paper_id,),
        ).fetchall()
        released = []
        for row in rows:
            busy = conn.execute(
                """SELECT id FROM busy_intervals
                   WHERE reviewer_id=? AND start_date<=? AND end_date>=? ORDER BY id LIMIT 1""",
                (row["reviewer_id"], paper["review_end"], paper["review_start"]),
            ).fetchone()
            if busy:
                self._release(conn, row["id"], paper_id, actor_id,
                              {"cause": "review_period_changed", "busy_interval_id": busy["id"]})
                released.append(row["id"])
        return released

    def _candidate_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, reviewer: sqlite3.Row) -> dict:
        """单个候选评审人的可邀请性与排除原因（与 assign 的检查保持一致）。"""
        reasons = []
        period_set = bool(paper["review_start"] and paper["review_end"])
        if not period_set:
            reasons.append({"code": "review_period_missing", "message": "主席尚未设定评审时段"})
        if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer["id"], paper["id"])).fetchone():
            reasons.append({"code": "conflict_of_interest", "message": "存在利益冲突"})
        existing = conn.execute(
            "SELECT status FROM assignments WHERE paper_id=? AND reviewer_id=? AND released_at IS NULL",
            (paper["id"], reviewer["id"]),
        ).fetchone()
        if existing:
            reasons.append({"code": "already_assigned", "message": f"已分配（状态 {existing['status']}）"})
        if period_set:
            busy = conn.execute(
                """SELECT start_date, end_date FROM busy_intervals
                   WHERE reviewer_id=? AND start_date<=? AND end_date>=? ORDER BY id LIMIT 1""",
                (reviewer["id"], paper["review_end"], paper["review_start"]),
            ).fetchone()
            if busy:
                reasons.append({
                    "code": "reviewer_unavailable",
                    "message": f"忙碌区间 {busy['start_date']}~{busy['end_date']} 与评审时段重叠",
                })
        load = self._active_load(conn, reviewer["id"])
        if load >= reviewer["load_limit"]:
            reasons.append({"code": "reviewer_at_capacity", "message": f"负载已满（{load}/{reviewer['load_limit']}）"})
        return {
            "reviewer_id": reviewer["id"],
            "name": reviewer["name"],
            "load": load,
            "load_limit": reviewer["load_limit"],
            "eligible": not reasons,
            "excluded_reasons": reasons,
        }
