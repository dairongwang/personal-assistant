from datetime import datetime, timezone


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_create_and_get_task(client):
    r = client.post("/tasks", json={"title": "  Pay rent ", "priority": "high", "tags": ["home", "Home", " bills "]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "Pay rent"  # trimmed
    assert body["tags"] == ["home", "bills"]  # de-duplicated case-insensitively, trimmed
    assert body["status"] == "todo" and body["completed_at"] is None

    r = client.get(f"/tasks/{body['id']}")
    assert r.status_code == 200 and r.json()["id"] == body["id"]


def test_blank_title_rejected(client):
    for title in ["", "   "]:
        assert client.post("/tasks", json={"title": title}).status_code == 422


def test_invalid_enum_values_rejected(client):
    assert client.post("/tasks", json={"title": "x", "priority": "critical"}).status_code == 422
    assert client.get("/tasks?status=bogus").status_code == 422
    assert client.get("/tasks?priority=bogus").status_code == 422


def test_due_date_timezone_is_preserved_as_utc(client):
    # A phone in UTC-7 sends its local 16:30 as UTC 23:30Z. It must come back unchanged.
    r = client.post("/tasks", json={"title": "UTC task", "due_date": "2030-10-15T23:30:00Z"})
    assert r.json()["due_date"] == "2030-10-15T23:30:00Z"

    # An explicit offset is normalised to UTC.
    r = client.post("/tasks", json={"title": "Offset task", "due_date": "2030-10-15T16:30:00-07:00"})
    assert r.json()["due_date"] == "2030-10-15T23:30:00Z"


def test_priority_ordering_is_by_meaning_not_alphabet(client):
    for p in ["medium", "urgent", "low", "high"]:
        client.post("/tasks", json={"title": p, "priority": p})
    titles = [t["title"] for t in client.get("/tasks").json()]
    assert titles == ["urgent", "high", "medium", "low"]


def test_due_tasks_come_before_undated(client):
    client.post("/tasks", json={"title": "no date", "priority": "urgent"})
    client.post("/tasks", json={"title": "dated", "priority": "low", "due_date": "2030-01-01T00:00:00Z"})
    titles = [t["title"] for t in client.get("/tasks").json()]
    assert titles == ["dated", "no date"]


def test_completed_at_tracks_status_transitions(client):
    task_id = client.post("/tasks", json={"title": "t"}).json()["id"]

    r = client.patch(f"/tasks/{task_id}", json={"status": "done"})
    assert r.status_code == 200 and r.json()["completed_at"] is not None
    completed_at = r.json()["completed_at"]

    # Editing a done task does not move completed_at.
    r = client.patch(f"/tasks/{task_id}", json={"title": "renamed"})
    assert r.json()["completed_at"] == completed_at

    # Re-opening clears it.
    r = client.patch(f"/tasks/{task_id}", json={"status": "todo"})
    assert r.json()["completed_at"] is None


def test_update_validation(client):
    task_id = client.post("/tasks", json={"title": "t"}).json()["id"]
    assert client.patch(f"/tasks/{task_id}", json={"title": " "}).status_code == 422
    assert client.patch(f"/tasks/{task_id}", json={"status": "nope"}).status_code == 422
    assert client.patch("/tasks/9999", json={"title": "x"}).status_code == 404


def test_delete_task(client):
    task_id = client.post("/tasks", json={"title": "t"}).json()["id"]
    assert client.delete(f"/tasks/{task_id}").status_code == 204
    assert client.get(f"/tasks/{task_id}").status_code == 404
    assert client.delete(f"/tasks/{task_id}").status_code == 404


def test_filters_search_and_pagination(client):
    for i in range(5):
        client.post("/tasks", json={"title": f"task {i}", "tags": ["even" if i % 2 == 0 else "odd"]})
    client.post("/tasks", json={"title": "Call dentist", "description": "about the crown", "status": "done"})

    assert len(client.get("/tasks?limit=2").json()) == 2
    assert len(client.get("/tasks?limit=2&offset=4").json()) == 2
    assert [t["title"] for t in client.get("/tasks?status=done").json()] == ["Call dentist"]
    assert [t["title"] for t in client.get("/tasks?q=crown").json()] == ["Call dentist"]
    assert len(client.get("/tasks?tag=EVEN").json()) == 3
    assert client.get("/tasks?limit=0").status_code == 422
