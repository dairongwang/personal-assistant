"""Browsers still running an older build of the app must be healed, not left with 404s."""


def test_old_service_worker_is_replaced_by_a_safety_worker(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert "unregister" in r.text and "caches.delete" in r.text and "navigate" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_old_endpoints_answer_410_with_an_explanation(client):
    for path in ("/update/status", "/tasks", "/today?tz=UTC", "/js/app.js", "/css/app.css"):
        r = client.get(path)
        assert r.status_code == 410, path
        assert "old version" in r.json()["detail"]
    assert client.post("/tasks", json={"title": "x"}).status_code == 410


def test_root_explains_where_the_app_is_when_not_serving_it(client):
    r = client.get("/")
    assert r.status_code == 200 and "API only" in r.text and "dev.py serve" in r.text


def test_root_redirects_to_the_dev_server_when_configured(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "DEV_WEB_URL", "http://localhost:4200/")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "http://localhost:4200/"
