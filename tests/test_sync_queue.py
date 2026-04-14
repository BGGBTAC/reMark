"""Tests for the sync_queue retry queue."""

from __future__ import annotations

import pytest

from src.sync.state import SyncState


@pytest.fixture
def state(tmp_path):
    s = SyncState(tmp_path / "state.db")
    yield s
    s.close()


class TestEnqueueAndDequeue:
    def test_enqueue_returns_id(self, state):
        qid = state.enqueue("process_document", doc_id="d1", payload="h1")
        assert qid > 0

    def test_dequeue_ready_returns_pending(self, state):
        state.enqueue("process_document", doc_id="d1")
        due = state.dequeue_ready()
        assert len(due) == 1
        assert due[0]["op_type"] == "process_document"
        assert due[0]["status"] == "pending"

    def test_dequeue_skips_future_next_attempt(self, state):
        qid = state.enqueue("process_document", doc_id="d1")
        # Force a future back-off
        state.mark_queue_failed(qid, "network timeout")
        due = state.dequeue_ready()
        assert due == []

    def test_priority_order(self, state):
        low = state.enqueue("process_document", doc_id="low", priority=0)
        high = state.enqueue("process_document", doc_id="high", priority=5)
        order = [r["id"] for r in state.dequeue_ready()]
        assert order.index(high) < order.index(low)


class TestStatusTransitions:
    def test_mark_done_moves_status(self, state):
        qid = state.enqueue("process_document", doc_id="d1")
        state.mark_queue_done(qid)
        rows = state.list_queue(status="done")
        assert len(rows) == 1 and rows[0]["id"] == qid

    def test_failed_bumps_attempts(self, state):
        qid = state.enqueue("process_document", doc_id="d1", max_attempts=3)
        state.mark_queue_failed(qid, "HTTP 500")
        state.mark_queue_failed(qid, "HTTP 500")
        entry = state.list_queue()[0]
        assert entry["attempts"] == 2
        assert entry["status"] == "pending"
        assert entry["last_error"] == "HTTP 500"

    def test_failed_caps_at_max_attempts(self, state):
        qid = state.enqueue("process_document", doc_id="d1", max_attempts=2)
        state.mark_queue_failed(qid, "boom")
        state.mark_queue_failed(qid, "boom")
        entry = state.list_queue()[0]
        assert entry["status"] == "failed"
        assert entry["attempts"] == 2

    def test_retry_entry_resets(self, state):
        qid = state.enqueue("process_document", doc_id="d1", max_attempts=1)
        state.mark_queue_failed(qid, "err")
        assert state.list_queue()[0]["status"] == "failed"

        state.retry_queue_entry(qid)
        entry = state.list_queue()[0]
        assert entry["status"] == "pending"
        assert entry["attempts"] == 0
        assert entry["last_error"] is None


class TestSummaryAndClear:
    def test_summary_groups_by_status(self, state):
        state.enqueue("process_document", doc_id="a")
        state.enqueue("process_document", doc_id="b")
        qid = state.enqueue("process_document", doc_id="c")
        state.mark_queue_done(qid)
        summary = state.queue_summary()
        assert summary.get("pending") == 2
        assert summary.get("done") == 1

    def test_clear_specific_status(self, state):
        keep = state.enqueue("process_document", doc_id="keep")
        done_id = state.enqueue("process_document", doc_id="gone")
        state.mark_queue_done(done_id)

        n = state.clear_queue(status="done")
        assert n == 1
        assert [r["id"] for r in state.list_queue()] == [keep]

    def test_clear_all(self, state):
        state.enqueue("process_document", doc_id="a")
        state.enqueue("process_document", doc_id="b")
        assert state.clear_queue() == 2
        assert state.list_queue() == []
