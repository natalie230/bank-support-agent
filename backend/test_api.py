"""Run: uv run python backend/test_api.py

Needs TEST_DATABASE_URL -- it drops and rebuilds that database's schema.
"""
import os

import db
import main
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()
url = os.environ.get("TEST_DATABASE_URL")
if not url:
    raise SystemExit("set TEST_DATABASE_URL to a scratch database first")

con = db.connect(url)
con.execute(db.SCHEMA.read_text())


def test_db():  # point the app at the scratch database
    with db.connect(url) as c:
        yield c


main.app.dependency_overrides[main.db] = test_db
client = TestClient(main.app)

one = lambda sql, *a: con.execute(sql, a).fetchone()["id"]  # noqa: E731
cust = one("INSERT INTO customers (name, email) VALUES ('Ana', 'a@x.com') RETURNING id")
lang = one("INSERT INTO languages (name) VALUES ('en') RETURNING id")
skill = one("INSERT INTO skills (name) VALUES ('fraud') RETURNING id")
agent = one("INSERT INTO agents (name, email, status) "
            "VALUES ('Priya', 'p@x.com', 'available') RETURNING id")
con.execute("INSERT INTO agent_skills VALUES (%s, %s)", (agent, skill))
con.execute("INSERT INTO agent_languages VALUES (%s, %s)", (agent, lang))

body = {"customer_id": cust, "language_id": lang, "skill_ids": [skill],
        "description": "card charged twice"}

r = client.post("/tickets", json=body)
assert r.status_code == 201, r.text
ticket = r.json()
assert ticket["status"] == "open", ticket
assert ticket["agent_id"] is None, ticket
assert ticket["skill_ids"] == [skill], ticket

r = client.get(f"/tickets/{ticket['id']}")
assert r.status_code == 200 and r.json() == ticket, r.text

r = client.post(f"/agents/{agent}/claim")
assert r.status_code == 200, r.text
claimed = r.json()
assert claimed["id"] == ticket["id"], claimed
assert claimed["status"] == "in_chat" and claimed["agent_id"] == agent, claimed

# nothing left to claim -> null, and the agent is at capacity anyway
assert client.post(f"/agents/{agent}/claim").json() is None

assert client.get("/tickets/999999").status_code == 404

# unknown customer id is a bad request, not a crash
r = client.post("/tickets", json={**body, "customer_id": 999999})
assert r.status_code == 400, r.text

# skill_ids must not be empty -- a skill-less ticket matches every agent
r = client.post("/tickets", json={**body, "skill_ids": []})
assert r.status_code == 422, r.text

other = one("INSERT INTO agents (name, email, status) "
            "VALUES ('Wei', 'w@x.com', 'available') RETURNING id")

# only the agent holding the ticket may close it
assert client.put(f"/tickets/{ticket['id']}/close",
                  json={"closed_by": other}).status_code == 409
assert client.put("/tickets/999999/close", json={"closed_by": agent}).status_code == 404

r = client.put(f"/tickets/{ticket['id']}/close", json={"closed_by": agent})
assert r.status_code == 200 and r.json()["status"] == "closed", r.text
# retrying the same close is a retry, not an error
assert client.put(f"/tickets/{ticket['id']}/close",
                  json={"closed_by": agent}).status_code == 200

# closing frees capacity -- without this the desk deadlocks after N tickets
assert client.get(f"/agents/{agent}/status").json() == {
    "id": agent, "status": "available", "capacity": 1, "active_tickets": 0}
second = client.post("/tickets", json=body).json()
assert client.post(f"/agents/{agent}/claim").json()["id"] == second["id"]

# going unavailable does not drop a chat already in progress
r = client.put(f"/agents/{agent}/status", json={"status": "unavailable"})
assert r.json() == {"id": agent, "status": "unavailable",
                    "capacity": 1, "active_tickets": 1}, r.text
assert client.get(f"/tickets/{second['id']}").json()["status"] == "in_chat"

# ...but no new work arrives while unavailable, even with capacity free
client.put(f"/tickets/{second['id']}/close", json={"closed_by": agent})
third = client.post("/tickets", json=body).json()
assert client.post(f"/agents/{agent}/claim").json() is None, "unavailable agent claimed"

client.put(f"/agents/{agent}/status", json={"status": "available"})
assert client.post(f"/agents/{agent}/claim").json()["id"] == third["id"]

assert client.get("/agents/999999/status").status_code == 404
assert client.put("/agents/999999/status",
                  json={"status": "available"}).status_code == 404

# the waiting room is queryable -- the whole reason ADR 0001 picked a table
assert client.get("/queue").json() == [], "in_chat ticket showed up as waiting"
normal = client.post("/tickets", json=body).json()
urgent = client.post("/tickets", json={**body, "urgency": "high"}).json()
q = client.get("/queue").json()
assert [t["id"] for t in q] == [urgent["id"], normal["id"]], q  # urgency beats age
assert q[0]["skill_ids"] == [skill], q

print("ok")
