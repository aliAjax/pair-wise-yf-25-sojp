"""分配事务模块：邀请、响应、休假冲突失效与候选排除原因。

所有函数都在调用方开启的事务（BEGIN IMMEDIATE）内运行，不自行提交；
由 app.ReviewStore 的薄封装负责鉴权、开事务与审计之外的收尾。
时段判定委托给 scheduling 模块，主席页面只消费这里的返回结构。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import scheduling
from errors import BusinessError

ACTIVE_STATUSES = ("invited", "accepted")  # 占用负载名额的状态
COVERAGE_STATUSES = ("accepted", "completed")  # 必须被同一评审人全程覆盖的状态


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _window_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "start": row["start"], "end": row["end"], "reason": row["reason"]}


def _load(conn: sqlite3.Connection, reviewer_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
        (reviewer_id,),
    ).fetchone()[0]


def _active_assignment_for(conn: sqlite3.Connection, paper_id: int, reviewer_id: str):
    return conn.execute(
        "SELECT * FROM assignments WHERE paper_id=? AND reviewer_id=? AND status IN ('invited','accepted')",
        (paper_id, reviewer_id),
    ).fetchone()


def _invalidate_one(
    conn: sqlite3.Connection, assignment_id: int, paper_id: int, reviewer_id: str, busy_id: int, actor: str
) -> None:
    conn.execute(
        "UPDATE assignments SET status='invalidated', invalidated_by=?, updated_at=? WHERE id=?",
        (busy_id, utcnow(), assignment_id),
    )
    conn.execute(
        "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (
            paper_id,
            actor,
            "assignment.invalidate",
            json.dumps(
                {"assignment_id": assignment_id, "reviewer_id": reviewer_id, "busy_period_id": busy_id},
                ensure_ascii=False,
                sort_keys=True,
            ),
            utcnow(),
        ),
    )


def invalidate_for_busy(conn: sqlite3.Connection, reviewer_id: str, busy: sqlite3.Row, actor: str) -> list[int]:
    """登记/调整忙碌区间后调用：把与冲突休假重叠的已覆盖分配失效并释放名额。

    只处理 accepted/completed 分配（invited 尚未承诺，接受时会重新校验）；
    已决定论文不动。评审意见与分数原样保留在分配记录上。
    """
    invalidated: list[int] = []
    rows = conn.execute(
        """SELECT a.id, a.paper_id, p.review_start, p.review_end
           FROM assignments a JOIN papers p ON p.id = a.paper_id
           WHERE a.reviewer_id=? AND a.status IN ('accepted','completed') AND p.status != 'decided'""",
        (reviewer_id,),
    ).fetchall()
    for row in rows:
        if row["review_start"] and row["review_end"] and scheduling.overlaps(
            row["review_start"], row["review_end"], busy["start"], busy["end"]
        ):
            _invalidate_one(conn, row["id"], row["paper_id"], reviewer_id, busy["id"], actor)
            invalidated.append(row["id"])
    return invalidated


def invalidate_paper_for_window(
    conn: sqlite3.Connection, paper_id: int, start: str, end: str, actor: str
) -> list[int]:
    """主席调整评审窗口后调用：失效本论文上与新窗口内任意忙碌区间冲突的覆盖分配。"""
    invalidated: list[int] = []
    rows = conn.execute(
        "SELECT * FROM assignments WHERE paper_id=? AND status IN ('accepted','completed')", (paper_id,)
    ).fetchall()
    for row in rows:
        busy = scheduling.conflicting_busy(conn, row["reviewer_id"], start, end)
        if busy:
            _invalidate_one(conn, row["id"], paper_id, row["reviewer_id"], busy[0]["id"], actor)
            invalidated.append(row["id"])
    return invalidated


def invite(conn: sqlite3.Connection, paper: sqlite3.Row, reviewer: sqlite3.Row) -> dict:
    """主席发出邀请。前置条件：论文可分配、评审窗口已设定、评审人全程有空且未满负载。"""
    if not paper["review_start"] or not paper["review_end"]:
        raise BusinessError("主席尚未为该论文设定评审起止时间", 409, "review_window_missing")
    existing = conn.execute(
        "SELECT * FROM assignments WHERE paper_id=? AND reviewer_id=?",
        (paper["id"], reviewer["id"]),
    ).fetchone()
    if existing and existing["status"] in ACTIVE_STATUSES:
        raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
    if existing and existing["status"] == "declined":
        raise BusinessError("该评审人已拒绝过此论文的邀请", 409, "assignment_declined")
    if conn.execute(
        "SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer["id"], paper["id"])
    ).fetchone():
        raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
    busy = scheduling.conflicting_busy(conn, reviewer["id"], paper["review_start"], paper["review_end"])
    if busy:
        raise BusinessError(
            f"评审人在评审周期内登记了忙碌区间（{busy[0]['start']} 至 {busy[0]['end']}），无法全程覆盖",
            409,
            "reviewer_unavailable",
        )
    if _load(conn, reviewer["id"]) >= reviewer["load_limit"]:
        raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
    now = utcnow()
    if existing:  # 此前被失效的记录：复活为新邀请，历史评审内容保留在记录上
        conn.execute(
            "UPDATE assignments SET status='invited', invalidated_by=NULL, updated_at=? WHERE id=?",
            (now, existing["id"]),
        )
        assignment_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
            (paper["id"], reviewer["id"], now, now),
        )
        assignment_id = cur.lastrowid
    conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper["id"],))
    return {"id": assignment_id, "paper_id": paper["id"], "reviewer_id": reviewer["id"], "status": "invited"}


def respond(conn: sqlite3.Connection, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
    """评审人响应邀请。接受时重新校验：评审周期内不得有冲突休假。"""
    row = conn.execute(
        "SELECT * FROM assignments WHERE id=?", (assignment_id,)
    ).fetchone()
    if not row or row["reviewer_id"] != reviewer_id:
        raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
    if row["status"] != "invited":
        raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
    if accepted:
        paper = conn.execute("SELECT * FROM papers WHERE id=?", (row["paper_id"],)).fetchone()
        busy = scheduling.conflicting_busy(conn, reviewer_id, paper["review_start"], paper["review_end"])
        if busy:
            raise BusinessError(
                f"评审周期与已登记的忙碌区间（{busy[0]['start']} 至 {busy[0]['end']}）冲突，无法接受",
                409,
                "reviewer_unavailable",
            )
    status = "accepted" if accepted else "declined"
    conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
    return {"id": assignment_id, "status": status}


def candidate_verdicts(
    conn: sqlite3.Connection, paper: sqlite3.Row, reviewers: list[sqlite3.Row]
) -> tuple[list[dict], dict[str, list[dict]]]:
    """计算一篇论文的候选评审人：可邀请列表 + 每个被排除者的原因列表。"""
    assignments = {
        row["reviewer_id"]: row
        for row in conn.execute("SELECT * FROM assignments WHERE paper_id=?", (paper["id"],)).fetchall()
    }
    conflicts = {
        row["reviewer_id"]: row["reason"]
        for row in conn.execute("SELECT * FROM conflicts WHERE paper_id=?", (paper["id"],)).fetchall()
    }
    window_set = bool(paper["review_start"] and paper["review_end"])
    eligible: list[dict] = []
    excluded: dict[str, list[dict]] = {}
    for reviewer in reviewers:
        reasons: list[dict] = []
        if not window_set:
            reasons.append({"code": "review_window_missing", "message": "主席尚未设定评审起止时间"})
        assignment = assignments.get(reviewer["id"])
        if assignment and assignment["status"] in ACTIVE_STATUSES:
            reasons.append({"code": "assignment_exists", "message": "已有进行中的分配"})
        if assignment and assignment["status"] == "declined":
            reasons.append({"code": "assignment_declined", "message": "已拒绝过该论文的邀请"})
        if reviewer["id"] in conflicts:
            reasons.append({"code": "conflict_of_interest", "message": f"存在利益冲突：{conflicts[reviewer['id']]}"})
        if window_set:
            busy = scheduling.conflicting_busy(conn, reviewer["id"], paper["review_start"], paper["review_end"])
            if busy:
                reasons.append(
                    {
                        "code": "reviewer_unavailable",
                        "message": f"评审周期与忙碌区间冲突（{busy[0]['start']} 至 {busy[0]['end']}，{busy[0]['reason']}）",
                    }
                )
        load = _load(conn, reviewer["id"])
        if load >= reviewer["load_limit"]:
            reasons.append(
                {"code": "reviewer_at_capacity", "message": f"负载已满（{load}/{reviewer['load_limit']}）"}
            )
        if reasons:
            excluded[reviewer["id"]] = reasons
        else:
            eligible.append({"id": reviewer["id"], "name": reviewer["name"], "load": load, "load_limit": reviewer["load_limit"]})
    return eligible, excluded
