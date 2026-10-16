def test_event_crud(client):
    r = client.post(
        "/events",
        json={"title": "Standup", "start_time": "2030-10-15T09:00:00Z", "end_time": "2030-10-15T09:15:00Z"},
    )
    assert r.status_code == 201, r.text
    event = r.json()

    r = client.patch(f"/events/{event['id']}", json={"title": "Daily standup", "location": "Room 1"})
    assert r.status_code == 200 and r.json()["title"] == "Daily standup" and r.json()["location"] == "Room 1"

    assert client.get(f"/events/{event['id']}").status_code == 200
    assert client.delete(f"/events/{event['id']}").status_code == 204
    assert client.get(f"/events/{event['id']}").status_code == 404
    assert client.patch(f"/events/{event['id']}", json={"title": "x"}).status_code == 404


def test_event_end_must_be_after_start(client):
    bad = {"title": "Bad", "start_time": "2030-10-15T15:00:00Z", "end_time": "2030-10-15T14:00:00Z"}
    assert client.post("/events", json=bad).status_code == 422
    same = {"title": "Zero", "start_time": "2030-10-15T15:00:00Z", "end_time": "2030-10-15T15:00:00Z"}
    assert client.post("/events", json=same).status_code == 422

    ok = client.post(
        "/events", json={"title": "Ok", "start_time": "2030-10-15T15:00:00Z", "end_time": "2030-10-15T16:00:00Z"}
    ).json()
    # Moving end before the existing start via PATCH is rejected too.
    assert client.patch(f"/events/{ok['id']}", json={"end_time": "2030-10-15T14:00:00Z"}).status_code == 422


def test_event_range_filter(client):
    client.post(
        "/events", json={"title": "A", "start_time": "2030-01-01T09:00:00Z", "end_time": "2030-01-01T10:00:00Z"}
    )
    client.post(
        "/events", json={"title": "B", "start_time": "2030-01-02T09:00:00Z", "end_time": "2030-01-02T10:00:00Z"}
    )
    r = client.get("/events?start=2030-01-01T12:00:00Z&end=2030-01-03T00:00:00Z")
    assert [e["title"] for e in r.json()] == ["B"]
