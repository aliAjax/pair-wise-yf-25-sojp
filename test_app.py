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

    def _paper_with_period(self, start="2026-10-01", end="2026-10-31"):
        paper_id = self._paper()
        self.store.set_review_period("chair", paper_id, start, end)
        return paper_id

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper_with_period()
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
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper_with_period()
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


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def _paper_with_period(self, start="2026-10-01", end="2026-10-31"):
        paper_id = self._paper()
        self.store.set_review_period("chair", paper_id, start, end)
        return paper_id

    def test_review_period_validation_and_chair_only(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_review_period("chair", paper_id, "2026-10-31", "2026-10-01")
        self.assertEqual(ctx.exception.code, "invalid_period")
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_review_period("chair", paper_id, "十月一日", "2026-10-01")
        self.assertEqual(ctx.exception.code, "invalid_date")
        with self.assertRaises(BusinessError) as ctx:
            self.store.set_review_period("r1", paper_id, "2026-10-01", "2026-10-31")
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_period_and_busy_interval_block_invitation(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "review_period_missing")
        self.store.set_review_period("chair", paper_id, "2026-10-01", "2026-10-31")
        self.store.add_busy_interval("r1", "2026-10-10", "2026-10-12", "休假")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "reviewer_unavailable")
        # 不重叠的忙碌区间不影响邀请。
        self.store.add_busy_interval("r2", "2026-11-01", "2026-11-05", "休假")
        self.store.assign("chair", paper_id, "r2")

    def test_vacation_after_acceptance_releases_and_frees_capacity(self):
        p1 = self._paper_with_period()
        p2 = self._paper_with_period()
        p3 = self._paper_with_period("2026-11-01", "2026-11-30")
        a1 = self.store.assign("chair", p1, "r3")["id"]  # r3 负载上限为 2
        a2 = self.store.assign("chair", p2, "r3")["id"]
        self.store.respond_assignment("r3", a1, True)
        self.store.respond_assignment("r3", a2, True)
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", p3, "r3")
        self.assertEqual(ctx.exception.code, "reviewer_at_capacity")
        # 接受后新增冲突休假：两份分配都失效，名额释放。
        result = self.store.add_busy_interval("r3", "2026-10-15", "2026-10-20", "休假")
        self.assertEqual(sorted(result["released_assignment_ids"]), sorted([a1, a2]))
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r3", a1, True)
        self.assertEqual(ctx.exception.code, "assignment_released")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_review("r3", a2, 4, "这份评审不应被接受。")
        self.assertEqual(ctx.exception.code, "assignment_released")
        # 名额已释放，可邀请到新论文（时段不与休假重叠）。
        self.store.assign("chair", p3, "r3")
        # 主席看到 p1 待补位，r3 因休假被排除并给出原因。
        overview = self.store.backfill_overview("chair")
        item = next(i for i in overview["items"] if i["paper_id"] == p1)
        self.assertTrue(item["needs_backfill"])
        self.assertEqual(item["released"], 1)
        cand = {c["reviewer_id"]: c for c in item["candidates"]}
        self.assertIn("reviewer_unavailable", [r["code"] for r in cand["r3"]["excluded_reasons"]])
        self.assertTrue(cand["r1"]["eligible"])

    def test_invited_assignment_released_and_reinvited_after_vacation_removed(self):
        paper_id = self._paper_with_period()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        interval = self.store.add_busy_interval("r1", "2026-10-01", "2026-10-31", "休假")
        self.assertEqual(interval["released_assignment_ids"], [a1])
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r1", a1, True)
        self.assertEqual(ctx.exception.code, "assignment_released")
        # 删除忙碌区间后主席可重新邀请同一评审人，复用原分配记录。
        self.store.delete_busy_interval("r1", interval["id"])
        again = self.store.assign("chair", paper_id, "r1")
        self.assertEqual(again["id"], a1)
        self.assertEqual(again["status"], "invited")

    def test_completed_reviews_and_decision_survive_new_vacation(self):
        paper_id = self._paper_with_period()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 5, "非常扎实的工作，建议直接接收。")
        self.store.submit_review("r2", a2, 4, "整体不错，建议补充实验细节。")
        self.store.decide("chair", paper_id, "accept", "达到接收标准。")
        # 已完成的评审不被释放，已作出的决定照常保留。
        result = self.store.add_busy_interval("r1", "2026-10-05", "2026-10-09", "休假")
        self.assertEqual(result["released_assignment_ids"], [])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        overview = self.store.backfill_overview("chair")
        self.assertNotIn(paper_id, [i["paper_id"] for i in overview["items"]])

    def test_extending_period_releases_conflicting_assignment(self):
        paper_id = self._paper_with_period("2026-10-01", "2026-10-10")
        self.store.add_busy_interval("r1", "2026-10-20", "2026-10-25", "休假")
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a1, True)
        result = self.store.set_review_period("chair", paper_id, "2026-10-01", "2026-10-31")
        self.assertEqual(result["released_assignment_ids"], [a1])

    def test_backfill_lists_exclusion_reasons_per_candidate(self):
        paper_id = self._paper_with_period()
        self.store.add_conflict("chair", paper_id, "r1", "合作作者")
        self.store.add_busy_interval("r2", "2026-10-20", "2026-10-25", "休假")
        overview = self.store.backfill_overview("chair")
        item = next(i for i in overview["items"] if i["paper_id"] == paper_id)
        self.assertTrue(item["needs_backfill"])
        self.assertEqual(item["shortage"], 2)
        cand = {c["reviewer_id"]: c for c in item["candidates"]}
        self.assertEqual([r["code"] for r in cand["r1"]["excluded_reasons"]], ["conflict_of_interest"])
        self.assertIn("reviewer_unavailable", [r["code"] for r in cand["r2"]["excluded_reasons"]])
        self.assertTrue(cand["r3"]["eligible"])
        # 非主席不能查看补位视图。
        with self.assertRaises(BusinessError) as ctx:
            self.store.backfill_overview("r1")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
