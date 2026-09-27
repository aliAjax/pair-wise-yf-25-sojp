import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def _window(self, paper_id, start="2026-10-01T09:00Z", end="2026-10-14T18:00Z"):
        self.store.set_review_window("chair", paper_id, start, end)

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        self._window(paper_id)
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 9)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self._window(paper_id)
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)


class AvailabilityAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.paper_id = self.store.submit_paper(
            "alice", "高可用事务复制协议", "本文研究跨地域事务复制的可用性，并给出完整的形式化证明与实验数据。"
        )["id"]
        self.store.set_review_window("chair", self.paper_id, "2026-10-01T09:00Z", "2026-10-14T18:00Z")

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_required_before_invite(self):
        pid = self.store.submit_paper("alice", "另一篇新论文", "这是一篇没有设定评审起止窗口的测试论文摘要内容。")["id"]
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", pid, "r1")
        self.assertEqual(ctx.exception.code, "review_window_missing")

    def test_invalid_window_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_review_window("chair", self.paper_id, "2026-10-15T09:00Z", "2026-10-14T18:00Z")
        self.assertEqual(ctx.exception.code, "invalid_review_window")

    def test_overlapping_busy_blocks_invite_adjacent_is_allowed(self):
        # 与评审周期相接（不重叠）的休假不算冲突。
        self.store.add_busy_period("r1", "2026-10-14T18:00Z", "2026-10-20T00:00Z", "休假")
        self.store.assign("chair", self.paper_id, "r1")
        # 真正落在周期内的休假则禁止邀请。
        self.store.add_busy_period("r2", "2026-10-10T00:00Z", "2026-10-12T00:00Z", "出差")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", self.paper_id, "r2")
        self.assertEqual(ctx.exception.code, "reviewer_unavailable")

    def test_busy_after_acceptance_invalidates_and_releases_load(self):
        a1 = self.store.assign("chair", self.paper_id, "r1")["id"]
        a2 = self.store.assign("chair", self.paper_id, "r2")["id"]
        a3 = self.store.assign("chair", self.paper_id, "r3")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.respond_assignment("r3", a3, True)
        self.store.submit_review("r1", a1, 5, "论文质量很高，理论与实验都非常扎实。")
        # r1 接受后才登记与周期冲突的休假（已有评审仍保留）；r3 同样失效以验证名额释放。
        result = self.store.add_busy_period("r1", "2026-10-05T00:00Z", "2026-10-06T00:00Z", "突发事假")
        self.assertEqual(result["invalidated"], [a1])
        result3 = self.store.add_busy_period("r3", "2026-10-07T00:00Z", "2026-10-08T00:00Z", "出差")
        self.assertEqual(result3["invalidated"], [a3])
        detail = self.store.chair_paper_detail("chair", self.paper_id)
        by_id = {a["id"]: a for a in detail["assignments"]}
        self.assertEqual(by_id[a1]["status"], "invalidated")
        self.assertEqual(by_id[a1]["score"], 5)  # 原评审意见照常保留。
        self.assertEqual(by_id[a2]["status"], "accepted")
        self.assertEqual(by_id[a3]["status"], "invalidated")
        # r3 负载上限为 2：失效释放名额后，可在两篇 11 月的新论文上各占一个名额。
        p_b = self.store.submit_paper("alice", "十一月论文甲", "用于验证失效释放负载名额的论文，摘要内容足够长。")["id"]
        self.store.set_review_window("chair", p_b, "2026-11-01T09:00Z", "2026-11-14T18:00Z")
        p_c = self.store.submit_paper("alice", "十一月论文乙", "用于验证失效释放负载名额的论文，摘要内容足够长。")["id"]
        self.store.set_review_window("chair", p_c, "2026-11-15T09:00Z", "2026-11-28T18:00Z")
        self.store.assign("chair", p_b, "r3")
        self.store.assign("chair", p_c, "r3")  # 若名额未释放，此处会 reviewer_at_capacity
        # 论文进入待补位列表。
        self.assertIn(self.paper_id, [p["id"] for p in self.store.attention_papers("chair")])
        history = self.store.history("chair", self.paper_id)
        self.assertEqual(sum(h["action"] == "assignment.invalidate" for h in history), 2)

    def test_busy_between_invite_and_accept_blocks_accept(self):
        a = self.store.assign("chair", self.paper_id, "r3")["id"]
        self.store.add_busy_period("r3", "2026-10-08T00:00Z", "2026-10-09T00:00Z", "休假")
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r3", a, True)
        self.assertEqual(ctx.exception.code, "reviewer_unavailable")

    def test_decided_paper_is_not_attention_and_not_invalidated(self):
        a1 = self.store.assign("chair", self.paper_id, "r1")["id"]
        a2 = self.store.assign("chair", self.paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比和讨论。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释清楚。")
        self.store.decide("chair", self.paper_id, "accept", "同意接收。")
        result = self.store.add_busy_period("r1", "2026-10-05T00:00Z", "2026-10-06T00:00Z", "事后休假")
        self.assertEqual(result["invalidated"], [])
        self.assertNotIn(self.paper_id, [p["id"] for p in self.store.attention_papers("chair")])

    def test_candidate_exclusion_reasons_and_capacity_release(self):
        self.store.add_busy_period("r2", "2026-10-03T00:00Z", "2026-10-04T00:00Z", "休假")
        self.store.add_conflict("chair", self.paper_id, "r3", "曾合著论文")
        a = self.store.assign("chair", self.paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a, False)
        # r3 负载限制为 2，用两篇无冲突论文填满。
        other = self.store.submit_paper("alice", "负载测试论文之二", "用于填满评审人负载的另一篇论文，摘要内容足够长。")["id"]
        self.store.set_review_window("chair", other, "2026-11-01T09:00Z", "2026-11-14T18:00Z")
        third = self.store.submit_paper("alice", "负载测试论文之三", "用于填满评审人负载的第三篇论文，摘要内容足够长。")["id"]
        self.store.set_review_window("chair", third, "2026-11-02T09:00Z", "2026-11-15T18:00Z")
        self.store.assign("chair", other, "r3")
        self.store.assign("chair", third, "r3")
        detail = self.store.chair_paper_detail("chair", self.paper_id)
        codes = {rid: {r["code"] for r in rs} for rid, rs in detail["candidates"]["excluded"].items()}
        self.assertEqual(codes["r1"], {"assignment_declined"})
        self.assertEqual(codes["r2"], {"reviewer_unavailable"})
        self.assertEqual(codes["r3"], {"conflict_of_interest", "reviewer_at_capacity"})
        self.assertEqual(detail["candidates"]["eligible"], [])

    def test_reinvite_after_window_change_reuses_assignment(self):
        a = self.store.assign("chair", self.paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a, True)
        self.store.add_busy_period("r1", "2026-10-05T00:00Z", "2026-10-06T00:00Z", "事假")
        # 休假仍在，原窗口内不能重新邀请。
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", self.paper_id, "r1")
        self.assertEqual(ctx.exception.code, "reviewer_unavailable")
        # 主席把评审窗口移到休假之后，失效记录复活为新邀请，历史评审保留。
        result = self.store.set_review_window("chair", self.paper_id, "2026-10-20T09:00Z", "2026-10-27T18:00Z")
        self.assertEqual(result["invalidated"], [])
        again = self.store.assign("chair", self.paper_id, "r1")
        self.assertEqual(again["id"], a)
        detail = self.store.chair_paper_detail("chair", self.paper_id)
        self.assertEqual(len(detail["assignments"]), 1)
        self.assertEqual(detail["assignments"][0]["status"], "invited")

    def test_only_owner_or_chair_sees_busy(self):
        self.store.add_busy_period("r1", "2026-12-01T00:00Z", "2026-12-02T00:00Z", "年假")
        self.assertEqual(len(self.store.list_busy_periods("r1", "r1")), 1)
        self.assertEqual(len(self.store.list_busy_periods("chair", "r1")), 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_busy_periods("r2", "r1")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
