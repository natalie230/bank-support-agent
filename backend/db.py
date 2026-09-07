import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

SCHEMA = Path(__file__).parent / "schema.sql"
SEED = Path(__file__).parent / "seed.sql"
load_dotenv()

# see docs/adr/0004-partial-matching-with-wait-threshold.md
SKILL_THRESHOLD = float(os.environ.get("SKILL_THRESHOLD", 1.0))
WAIT_THRESHOLD_SECONDS = float(os.environ.get("WAIT_THRESHOLD_SECONDS", 120))
# see docs/adr/0005-claim-expiry-releases-abandoned-tickets.md
CLAIM_EXPIRY_SECONDS = float(os.environ.get("CLAIM_EXPIRY_SECONDS", 3600))


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or dsn(), row_factory=dict_row, autocommit=True)


def release_stale(
    con: psycopg.Connection, expiry_seconds: float | None = None
) -> list[int]:
    """Send tickets whose agent went dark back to the waiting room.

    ADR 0001 traded SQS's visibility timeout for writing this ourselves. A
    released ticket keeps its original created_at, so it comes back old and
    ranks ahead of fresh arrivals rather than starting its wait over.

    ponytail: time-based, because nothing in this system reports agent
    liveness. A real chat running past the expiry gets pulled out from under a
    working agent -- the fix is an agent heartbeat, not a bigger number.
    """
    rows = con.execute(
        """
        UPDATE tickets SET status = 'open', agent_id = NULL, claimed_at = NULL
        WHERE status = 'in_chat'
          AND claimed_at <= now() - make_interval(secs => %(expiry)s)
        RETURNING id
        """,
        {"expiry": CLAIM_EXPIRY_SECONDS if expiry_seconds is None else expiry_seconds},
    ).fetchall()
    return [r["id"] for r in rows]


def claim(
    con: psycopg.Connection,
    agent_id: int,
    skill_threshold: float | None = None,
    wait_seconds: float | None = None,
) -> int | None:
    """Take the top claimable ticket for this agent. None if nothing to take.

    A ticket is claimable when the agent covers at least `skill_threshold` of
    its required skills -- OR when it has waited longer than `wait_seconds`, at
    which point any available agent will do. Starving tickets sort first, so a
    steady stream of well-matched work cannot keep one waiting past its bound.

    Language is a hard filter at every threshold: the agent must speak one of
    the languages the customer listed. Speaking none is not a bad match, it is
    no match.
    """
    # ponytail: reap on poll -- no scheduler, no extra process. Nothing to
    # release when nobody wants work anyway.
    release_stale(con)
    params = {
        "me": agent_id,
        "floor": SKILL_THRESHOLD if skill_threshold is None else skill_threshold,
        "wait": WAIT_THRESHOLD_SECONDS if wait_seconds is None else wait_seconds,
    }
    row = con.execute(
        """
        UPDATE tickets SET status = 'in_chat', agent_id = %(me)s, claimed_at = now()
        WHERE id = (
          SELECT t.id FROM tickets t
          WHERE t.status = 'open'
            AND EXISTS (
              SELECT 1 FROM ticket_languages tl JOIN agent_languages al USING (language_id)
              WHERE tl.ticket_id = t.id AND al.agent_id = %(me)s)
            AND EXISTS (
              SELECT 1 FROM agents a WHERE a.id = %(me)s AND a.status = 'available'
                AND (SELECT count(*) FROM tickets
                     WHERE agent_id = a.id AND status = 'in_chat') < a.capacity
            )
            AND (
              COALESCE((
                SELECT (count(*) FILTER (WHERE ts.skill_id IN (
                          SELECT skill_id FROM agent_skills WHERE agent_id = %(me)s)))::numeric
                     / NULLIF(count(*), 0)
                FROM ticket_skills ts WHERE ts.ticket_id = t.id
              ), 1) >= %(floor)s::numeric
              OR t.created_at <= now() - make_interval(secs => %(wait)s)
            )
          -- starving first, so a good-match stream cannot starve a poor match
          ORDER BY (t.created_at <= now() - make_interval(secs => %(wait)s)) DESC,
                   t.urgency DESC, t.created_at
          LIMIT 1
          FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        params,
    ).fetchone()
    return row["id"] if row else None


def waiting(con: psycopg.Connection, wait_seconds: float | None = None) -> list[dict]:
    """The waiting room, ranked exactly as claim() takes from it.

    Count is the queue depth; the head's created_at is the longest wait. Both
    questions ADR 0001 chose a table in order to be able to ask.
    """
    return con.execute(
        """
        SELECT t.*, c.name AS customer_name,
               (SELECT COALESCE(array_agg(skill_id), '{}'::bigint[])
                FROM ticket_skills WHERE ticket_id = t.id) AS skill_ids,
               (SELECT COALESCE(array_agg(language_id), '{}'::bigint[])
                FROM ticket_languages WHERE ticket_id = t.id) AS language_ids
        FROM tickets t
        JOIN customers c ON c.id = t.customer_id
        WHERE t.status = 'open'
        -- same ranking as claim() above; they must not disagree
        ORDER BY (t.created_at <= now() - make_interval(secs => %(wait)s)) DESC,
                 t.urgency DESC, t.created_at
        """,
        {"wait": WAIT_THRESHOLD_SECONDS if wait_seconds is None else wait_seconds},
    ).fetchall()


if __name__ == "__main__":  # DESTRUCTIVE: drops and rebuilds the whole schema
    target = dsn().rsplit("/", 1)[-1]
    if input(f"drop and rebuild every table in {target}? [y/N] ").lower() != "y":
        raise SystemExit("cancelled")
    with connect() as con:
        con.execute(SCHEMA.read_text())
        con.execute(SEED.read_text())
    print(f"schema rebuilt and seeded in {target}")
