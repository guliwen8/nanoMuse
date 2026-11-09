from __future__ import annotations

from pathlib import Path

import httpx
import openai

from nanomuse.llm import MockLLM
from nanomuse.server.outbox import DeliveryOutbox
from nanomuse.server.service import MuseService


def _claim(store: DeliveryOutbox):
    return store.claim(
        "reminder:r_1:2026-10-23T09:00:00+00:00",
        kind="reminder",
        source_id="r_1",
        occurrence="2026-10-23T09:00:00+00:00",
        thread="main",
        text="run it",
        label="Reminder: run it",
    )


def test_pending_delivery_recovers_from_a_crash_before_the_ack(tmp_path: Path):
    """Queue insertion and execution are deliberately outside SQLite: a crash after the
    claim, after the enqueue or during the run leaves the same durable pending row, and
    the next start replays it once."""
    path = tmp_path / "outbox.db"
    store = DeliveryOutbox(path)
    claimed = _claim(store)
    assert claimed is not None
    store.close()

    recovered = DeliveryOutbox(path)
    assert recovered.pending() == [claimed]
    assert _claim(recovered) is None, "the occurrence idempotency key is stable"
    recovered.close()


def test_tool_requested_delivery_is_not_replayed_after_a_crash(tmp_path: Path):
    path = tmp_path / "outbox.db"
    store = DeliveryOutbox(path)
    claimed = _claim(store)
    assert claimed is not None
    running = store.start(claimed.id)
    assert running is not None
    store.mark_tool_requested(running.id)
    store.close()

    recovered = DeliveryOutbox(path)
    stopped = recovered.recover_after_tool_request()
    assert [delivery.id for delivery in stopped] == [claimed.id]
    assert stopped[0].status == "done" and stopped[0].tool_requested
    assert stopped[0].last_error == "interrupted after tool request"
    assert recovered.recover() == []
    assert [delivery.id for delivery in recovered.unfinalized()] == [claimed.id]
    recovered.close()


def test_completed_delivery_is_not_replayed_and_source_ack_can_resume(tmp_path: Path):
    path = tmp_path / "outbox.db"
    store = DeliveryOutbox(path)
    claimed = _claim(store)
    assert claimed is not None
    completed = store.complete(claimed.id)
    assert completed is not None and completed.id == claimed.id and completed.status == "done"
    store.close()

    recovered = DeliveryOutbox(path)
    assert recovered.pending() == []
    assert [delivery.id for delivery in recovered.unfinalized()] == [claimed.id]
    recovered.finalized(claimed.id)
    assert recovered.unfinalized() == []
    recovered.close()


def test_model_only_delivery_failure_is_deferred_but_tool_work_is_not_replayed(settings):
    """A scheduled run gets a bounded retry before tools, never after one was requested."""
    service = MuseService(settings, llm=MockLLM([]))
    claimed = _claim(service.deliveries)
    assert claimed is not None
    running = service.deliveries.start(claimed.id)
    assert running is not None and running.attempts == 1
    timeout = openai.APITimeoutError(httpx.Request("POST", "https://model.example/v1"))

    deferred = service._retry_delivery(claimed.id, timeout, tool_calls=0)
    assert deferred is not None
    assert deferred.attempts == 1 and deferred.last_error == "timeout"
    assert deferred.next_attempt_at and service.deliveries.pending() == []

    # Make the deferred attempt due without waiting sixty seconds in a unit test.
    service.deliveries._conn.execute(
        "UPDATE scheduled_deliveries SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (claimed.id,),
    )
    service.deliveries._conn.commit()
    retry = service.deliveries.start(claimed.id)
    assert retry is not None and retry.attempts == 2
    assert service._retry_delivery(claimed.id, timeout, tool_calls=1) is None
    assert service.deliveries.get(claimed.id).status == "running"  # type: ignore[union-attr]

    service._complete_delivery(claimed.id)
    assert service.deliveries.get(claimed.id).status == "done"  # type: ignore[union-attr]
    service.deliveries.close()
