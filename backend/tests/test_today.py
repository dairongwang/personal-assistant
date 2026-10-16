"""The summary must be computed for the *client's* day, not the server's."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_today_uses_client_timezone(client):
    la = ZoneInfo("America/Los_Angeles")
    now_la = datetime.now(la)
    # 23:30 tonight in Los Angeles is already "tomorrow" in UTC for most of the day.
    late_tonight = now_la.replace(hour=23, minute=30, second=0, microsecond=0)
    client.post("/tasks", json={"title": "late tonight (LA)", "due_date": iso(late_tonight)})

    r = client.get("/today?tz=America/Los_Angeles")
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "America/Los_Angeles"
    assert body["date"] == now_la.date().isoformat()
    assert [t["title"] for t in body["tasks_due_today"]] == ["late tonight (LA)"]
    assert body["overdue_tasks"] == []


def test_today_accepts_header_and_rejects_unknown_zone(client):
    r = client.get("/today", headers={"X-Timezone": "Asia/Tokyo"})
    assert r.status_code == 200 and r.json()["timezone"] == "Asia/Tokyo"
    assert client.get("/today?tz=Mars/Olympus").status_code == 422


def test_overdue_and_completed_today(client):
    tz = ZoneInfo("Europe/Berlin")
    now = datetime.now(tz)
    yesterday = now - timedelta(days=1)
    client.post("/tasks", json={"title": "yesterday", "due_date": iso(yesterday)})
    done_id = client.post("/tasks", json={"title": "finish me"}).json()["id"]
    client.patch(f"/tasks/{done_id}", json={"status": "done"})
    client.post(
        "/events",
        json={
            "title": "today event",
            "start_time": iso(now.replace(hour=12, minute=0, second=0, microsecond=0)),
            "end_time": iso(now.replace(hour=13, minute=0, second=0, microsecond=0)),
        },
    )

    body = client.get("/today?tz=Europe/Berlin").json()
    assert [t["title"] for t in body["overdue_tasks"]] == ["yesterday"]
    assert body["completed_today"] == 1
    assert body["total_open_tasks"] == 1
    assert [e["title"] for e in body["upcoming_events"]] == ["today event"]


def test_editing_an_old_done_task_does_not_count_as_completed_today(client):
    """Regression: the old implementation used updated_at as a proxy for completion."""
    from sqlalchemy import text

    import database

    task_id = client.post("/tasks", json={"title": "old", "status": "done"}).json()["id"]
    with database.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET completed_at = '2020-01-01 10:00:00' WHERE id = :id"), {"id": task_id})
    client.patch(f"/tasks/{task_id}", json={"title": "old (renamed)"})
    assert client.get("/today").json()["completed_today"] == 0
