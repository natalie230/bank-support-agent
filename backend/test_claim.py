"""Run: uv run python backend/test_claim.py

Needs TEST_DATABASE_URL pointing at a scratch database -- every setup() call
drops and rebuilds its whole schema.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import psycopg

import db
from db import SCHEMA, claim, connect
from dotenv import load_dotenv

load_dotenv()
url = os.environ.get("TEST_DATABASE_URL")
if not url:
    raise SystemExit("set TEST_DATABASE_URL to a scratch database first")

db.SKILL_THRESHOLD = 1.0        # full coverage unless a test says otherwise
db.WAIT_THRESHOLD_SECONDS = 10_000
db.CLAIM_EXPIRY_SECONDS = 600   # tests backdate past this to force a release

AGENTS = [  # name, status, capacity, skills, languages
    ("Priya", "available", 1, ["fraud"], ["en"]),
    ("Wei", "available", 1, ["fraud"], ["en"]),
    ("Sam", "available", 1, ["mortgage"], ["en"]),
    ("Off", "unavailable", 1, ["fraud"], ["en"]),
    ("Multi", "available", 1, ["fraud", "mortgage"], ["en"]),
]


def setup():
    """Fresh schema, fresh connection, reference data loaded."""
    con = connect(url)
    con.execute(SCHEMA.read_text())
    one = lambda sql, *a: con.execute(sql, a).fetchone()["id"]  # noqa: E731

    cust = one("INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
               "Ana", "ana@x.com")
    skill = {n: one("INSERT INTO skills (name) VALUES (%s) RETURNING id", n)
             for n in ("fraud", "mortgage")}
    lang = {n: one("INSERT INTO languages (name) VALUES (%s) RETURNING id", n)
            for n in ("en", "zh")}

    agent = {}
    for name, status, cap, skills, langs in AGENTS:
        agent[name] = one(
            "INSERT INTO agents (name, email, status, capacity)"
            " VALUES (%s, %s, %s::agent_status, %s) RETURNING id",
            name, f"{name}@x.com", status, cap)
        for s in skills:
            con.execute("INSERT INTO agent_skills VALUES (%s, %s)", (agent[name], skill[s]))
        for lg in langs:
            con.execute("INSERT INTO agent_languages VALUES (%s, %s)", (agent[name], lang[lg]))

    def add_ticket(skills=("fraud",), urgency="normal", languages=("en",), age_seconds=0):
        tid = one("INSERT INTO tickets (customer_id, urgency, created_at)"
                  " VALUES (%s, %s::urgency_level,"
                  "         now() - make_interval(secs => %s)) RETURNING id",
                  cust, urgency, age_seconds)
        for s in skills:
            con.execute("INSERT INTO ticket_skills VALUES (%s, %s)", (tid, skill[s]))
        for lg in languages:
            con.execute("INSERT INTO ticket_languages VALUES (%s, %s)", (tid, lang[lg]))
        return tid

    return con, agent, add_ticket


def race(agent_ids):
    """Claim concurrently on separate connections."""
    def go(aid):
        with connect(url) as c:
            return claim(c, aid)
    with ThreadPoolExecutor(len(agent_ids)) as pool:
        return [f.result() for f in [pool.submit(go, a) for a in agent_ids]]


# one open ticket, two eligible agents racing: exactly one wins
con, agent, add_ticket = setup()
tid = add_ticket()
got = race([agent["Priya"], agent["Wei"]])
assert sorted(got, key=lambda x: x is None) == [tid, None], got

# two open tickets, two agents: SKIP LOCKED means neither blocks, both get one
con, agent, add_ticket = setup()
a, b = add_ticket(), add_ticket()
got = race([agent["Priya"], agent["Wei"]])
assert sorted(got) == sorted([a, b]), got

# wrong skill, unavailable agent, and over-capacity agent all get nothing
con, agent, add_ticket = setup()
add_ticket()
add_ticket()  # a second one, so the capacity assert below is not vacuous
assert claim(con, agent["Sam"]) is None, "mortgage agent took a fraud ticket"
assert claim(con, agent["Off"]) is None, "unavailable agent took a ticket"
assert claim(con, agent["Priya"]) is not None
assert claim(con, agent["Priya"]) is None, "agent claimed past capacity=1"

# a ticket needing two skills goes only to an agent holding both
con, agent, add_ticket = setup()
both = add_ticket(skills=("fraud", "mortgage"))
assert claim(con, agent["Priya"]) is None, "fraud-only agent took a fraud+mortgage ticket"
assert claim(con, agent["Sam"]) is None, "mortgage-only agent took a fraud+mortgage ticket"
assert claim(con, agent["Multi"]) == both

# urgency beats age
con, agent, add_ticket = setup()
add_ticket(urgency="normal")
urgent = add_ticket(urgency="high")
assert claim(con, agent["Priya"]) == urgent

# language must match too
con, agent, add_ticket = setup()
add_ticket(languages=("zh",))
assert claim(con, agent["Priya"]) is None, "en-only agent took a zh ticket"

# ...but any one shared language will do
con, agent, add_ticket = setup()
either = add_ticket(languages=("zh", "en"))
assert claim(con, agent["Priya"]) == either, "en agent skipped a zh-or-en ticket"

# a lower threshold lets a half-matched agent take a two-skill ticket
con, agent, add_ticket = setup()
both = add_ticket(skills=("fraud", "mortgage"))
assert claim(con, agent["Priya"], skill_threshold=0.5) == both

# past the wait threshold, a poor match gets taken anyway
con, agent, add_ticket = setup()
old = add_ticket(skills=("fraud", "mortgage"), age_seconds=300)
assert claim(con, agent["Priya"], wait_seconds=10_000) is None, "300s is not past 10000s"
assert claim(con, agent["Priya"], wait_seconds=120) == old

# a starving poor match outranks a fresh, high-urgency, perfect match
con, agent, add_ticket = setup()
starving = add_ticket(skills=("fraud", "mortgage"), age_seconds=300)
add_ticket(skills=("fraud",), urgency="high")
assert claim(con, agent["Priya"], wait_seconds=120) == starving, "urgency jumped the wait bound"

# email uniqueness ignores case: setup() already inserted ana@x.com
try:
    con.execute("INSERT INTO customers (name, email) VALUES ('Ana again', 'ANA@X.COM')")
    raise AssertionError("ANA@X.COM was accepted alongside ana@x.com")
except psycopg.errors.UniqueViolation:
    pass

# an agent who goes dark does not hold the ticket -- or their capacity -- forever
con, agent, add_ticket = setup()
tid = add_ticket()
assert claim(con, agent["Priya"]) == tid
assert claim(con, agent["Wei"]) is None, "fresh claim was released early"
con.execute("UPDATE tickets SET claimed_at = now() - make_interval(secs => 1200)"
            " WHERE id = %s", (tid,))
assert claim(con, agent["Wei"]) == tid, "stale claim was never released"
assert con.execute("SELECT count(*) AS n FROM tickets WHERE agent_id = %s",
                   (agent["Priya"],)).fetchone()["n"] == 0, "Priya still holds capacity"

# a released ticket keeps its age, so it outranks anything that arrived meanwhile
con, agent, add_ticket = setup()
old = add_ticket(age_seconds=300)
assert claim(con, agent["Priya"]) == old
con.execute("UPDATE tickets SET claimed_at = now() - make_interval(secs => 1200)"
            " WHERE id = %s", (old,))
add_ticket(urgency="high")  # fresh arrival that would win on urgency alone
assert claim(con, agent["Wei"], wait_seconds=120) == old, "released ticket restarted its wait"

print("ok")
