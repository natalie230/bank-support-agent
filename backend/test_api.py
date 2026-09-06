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
con.execute(db.SEED.read_text())  # the roster the app actually ships with


def test_db():  # point the app at the scratch database
    with db.connect(url) as c:
        yield c


main.app.dependency_overrides[main.db] = test_db
client = TestClient(main.app)

one = lambda sql, *a: con.execute(sql, a).fetchone()["id"]  # noqa: E731
cust = one("INSERT INTO customers (name, email) VALUES ('Ana', 'a@x.com') RETURNING id")
lang = one("SELECT id FROM languages WHERE name = 'English'")
skill = one("SELECT id FROM skills WHERE name = 'fraud'")
# Priya: fraud + cards, English + Tamil, capacity 1 -- see seed.sql
agent = one("UPDATE agents SET status = 'available'"
            " WHERE email = 'priya@bank.example' RETURNING id")

body = {"customer_id": cust, "language_ids": [lang], "skill_ids": [skill],
        "description": "card charged twice"}

r = client.post("/tickets", json=body)
assert r.status_code == 201, r.text
ticket = r.json()
assert ticket["status"] == "open", ticket
assert ticket["agent_id"] is None, ticket
assert ticket["skill_ids"] == [skill], ticket
assert ticket["language_ids"] == [lang], ticket
assert ticket["urgency"] == "high", ticket  # fraud is an urgent skill; nobody asked

r = client.get(f"/tickets/{ticket['id']}")
assert r.status_code == 200 and r.json() == ticket, r.text

r = client.post(f"/agents/{agent}/claim")
assert r.status_code == 200, r.text
claimed = r.json()
assert claimed["id"] == ticket["id"], claimed
assert claimed["status"] == "in_chat" and claimed["agent_id"] == agent, claimed

# nothing left to claim -> null, and the agent is at capacity anyway
assert client.post(f"/agents/{agent}/claim").json() is None

# the chat itself: both sides post, everyone reads it back in order
msgs = f"/tickets/{ticket['id']}/messages"
r = client.post(msgs, json={"sender": "customer", "body": "hello?"})
assert r.status_code == 201 and r.json()["sender"] == "customer", r.text
client.post(msgs, json={"sender": "agent", "body": "hi, looking at it now"})
assert [m["body"] for m in client.get(msgs).json()] == ["hello?", "hi, looking at it now"]
assert client.post(msgs, json={"sender": "agent", "body": ""}).status_code == 422
assert client.get("/tickets/999999/messages").status_code == 404

assert client.get("/tickets/999999").status_code == 404

# unknown customer id is a bad request, not a crash
r = client.post("/tickets", json={**body, "customer_id": 999999})
assert r.status_code == 400, r.text

# skill_ids must not be empty -- a skill-less ticket matches every agent
r = client.post("/tickets", json={**body, "skill_ids": []})
assert r.status_code == 422, r.text

other = one("SELECT id FROM agents WHERE email = 'wei@bank.example'")

# only the agent holding the ticket may close it
assert client.put(f"/tickets/{ticket['id']}/close",
                  json={"closed_by": other}).status_code == 409
assert client.put("/tickets/999999/close", json={"closed_by": agent}).status_code == 404

r = client.put(f"/tickets/{ticket['id']}/close", json={"closed_by": agent})
assert r.status_code == 200 and r.json()["status"] == "closed", r.text
# retrying the same close is a retry, not an error
assert client.put(f"/tickets/{ticket['id']}/close",
                  json={"closed_by": agent}).status_code == 200
assert client.post(msgs, json={"sender": "customer", "body": "wait"}).status_code == 409

# closing frees capacity -- without this the desk deadlocks after N tickets
assert client.get(f"/agents/{agent}/status").json() == {
    "id": agent, "name": "Priya", "status": "available",
    "capacity": 1, "active_ticket_ids": []}
second = client.post("/tickets", json=body).json()
assert client.post(f"/agents/{agent}/claim").json()["id"] == second["id"]

# going unavailable does not drop a chat already in progress
r = client.put(f"/agents/{agent}/status", json={"status": "unavailable"})
assert r.json() == {"id": agent, "name": "Priya", "status": "unavailable",
                    "capacity": 1, "active_ticket_ids": [second["id"]]}, r.text
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
cards = one("SELECT id FROM skills WHERE name = 'cards'")  # not urgent, per seed.sql
normal = client.post("/tickets", json={**body, "skill_ids": [cards]}).json()
urgent = client.post("/tickets", json=body).json()
assert (normal["urgency"], urgent["urgency"]) == ("normal", "high"), (normal, urgent)
q = client.get("/queue").json()
assert [t["id"] for t in q] == [urgent["id"], normal["id"]], q  # urgency beats age
assert q[0]["skill_ids"] == [skill], q
# ...and asking nicely does not help
assert client.post("/tickets", json={**body, "skill_ids": [cards], "urgency": "high"}
                   ).json()["urgency"] == "normal", "customer set their own urgency"

# reference data the sign-up forms read
skills = client.get("/skills").json()
languages = client.get("/languages").json()
assert {s["name"] for s in skills} >= {"fraud", "mortgage"}, skills
assert {x["name"] for x in languages} >= {"English", "Tamil"}, languages

# same email is the same customer, not a second row -- and case does not matter
c1 = client.post("/customers", json={"name": "Bo", "email": "bo@x.com"}).json()
c2 = client.post("/customers", json={"name": "Bo Tan", "email": "BO@X.COM"}).json()
assert c1["id"] == c2["id"] and c2["name"] == "Bo Tan", (c1, c2)
assert client.post("/customers", json={"name": "Bo", "email": "nope"}).status_code == 422

# a new hire lands on the roster but not on the phones
hire = client.post("/agents", json={"name": "Kai", "email": "kai@bank.example",
                                    "skill_ids": [skill], "language_ids": [lang],
                                    "capacity": 2}).json()
assert hire["status"] == "unavailable" and hire["capacity"] == 2, hire
assert hire["active_ticket_ids"] == [], hire
assert hire["id"] in [a["id"] for a in client.get("/agents").json()]
assert client.post("/agents", json={"name": "No skills", "email": "x@bank.example",
                                    "skill_ids": [], "language_ids": [lang]}
                   ).status_code == 422

# a bad skill id rolls the whole agent back -- no half-made agent on the roster
before = len(client.get("/agents").json())
assert client.post("/agents", json={"name": "Ghost", "email": "ghost@bank.example",
                                    "skill_ids": [999999], "language_ids": [lang]}
                   ).status_code == 400
assert len(client.get("/agents").json()) == before, "half-made agent survived"

print("ok")
