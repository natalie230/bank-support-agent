from datetime import datetime
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from db import claim, connect, waiting

app = FastAPI(title="bank-support-agent")


def db():
    with connect() as con:
        yield con


class TicketIn(BaseModel):
    customer_id: int
    language_id: int
    skill_ids: list[int] = Field(min_length=1)
    description: str = ""
    # ponytail: the ERD derives urgency from the ticket's skills, but those
    # rules do not exist yet. Taking it as input until they do.
    urgency: Literal["normal", "high"] = "normal"


class Ticket(BaseModel):
    id: int
    customer_id: int
    agent_id: int | None
    language_id: int
    status: Literal["open", "in_chat", "closed"]
    urgency: Literal["normal", "high"]
    description: str
    created_at: datetime
    skill_ids: list[int]


@app.exception_handler(psycopg.IntegrityError)
def integrity_error(request, exc: psycopg.IntegrityError):
    """Unknown customer/language/skill id is a bad request, not a 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc).splitlines()[0]})


def fetch(con: psycopg.Connection, ticket_id: int) -> Ticket:
    row = con.execute(
        """
        SELECT t.*, COALESCE(array_agg(ts.skill_id)
                     FILTER (WHERE ts.skill_id IS NOT NULL), '{}'::bigint[]) AS skill_ids
        FROM tickets t LEFT JOIN ticket_skills ts ON ts.ticket_id = t.id
        WHERE t.id = %s GROUP BY t.id
        """,
        (ticket_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**row)


@app.post("/tickets", status_code=201)
def create_ticket(payload: TicketIn, con: psycopg.Connection = Depends(db)) -> Ticket:
    with con.transaction():
        ticket_id = con.execute(
            "INSERT INTO tickets (customer_id, language_id, urgency, description)"
            " VALUES (%s, %s, %s::urgency_level, %s) RETURNING id",
            (payload.customer_id, payload.language_id, payload.urgency, payload.description),
        ).fetchone()["id"]
        con.execute(
            "INSERT INTO ticket_skills SELECT %s, unnest(%s::bigint[])",
            (ticket_id, payload.skill_ids),
        )
    return fetch(con, ticket_id)


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, con: psycopg.Connection = Depends(db)) -> Ticket:
    return fetch(con, ticket_id)


@app.get("/queue")
def get_queue(con: psycopg.Connection = Depends(db)) -> list[Ticket]:
    """The waiting room, ranked as claim() would take from it."""
    return [Ticket(**row) for row in waiting(con)]


@app.post("/agents/{agent_id}/claim")
def claim_ticket(agent_id: int, con: psycopg.Connection = Depends(db)) -> Ticket | None:
    """Poll for work. null means nothing claimable right now -- poll again."""
    ticket_id = claim(con, agent_id)
    return fetch(con, ticket_id) if ticket_id else None


class CloseIn(BaseModel):
    closed_by: int


@app.put("/tickets/{ticket_id}/close")
def close_ticket(
    ticket_id: int, payload: CloseIn, con: psycopg.Connection = Depends(db)
) -> Ticket:
    """Finish a chat. Frees the agent's capacity so they can claim again."""
    closed = con.execute(
        "UPDATE tickets SET status = 'closed'"
        " WHERE id = %s AND agent_id = %s AND status = 'in_chat' RETURNING id",
        (ticket_id, payload.closed_by),
    ).fetchone()
    if closed:
        return fetch(con, ticket_id)

    ticket = fetch(con, ticket_id)  # raises 404 if it does not exist
    if ticket.status == "closed" and ticket.agent_id == payload.closed_by:
        return ticket  # already closed by this agent -- a retry, not an error
    raise HTTPException(409, f"ticket is {ticket.status} and held by {ticket.agent_id}")


class AgentStatusIn(BaseModel):
    status: Literal["available", "unavailable"]


class AgentStatus(BaseModel):
    id: int
    status: Literal["available", "unavailable"]
    capacity: int
    active_tickets: int


AGENT_STATUS_COLUMNS = """
  id, status, capacity,
  (SELECT count(*) FROM tickets t
   WHERE t.agent_id = agents.id AND t.status = 'in_chat') AS active_tickets
"""


@app.get("/agents/{agent_id}/status")
def get_agent_status(agent_id: int, con: psycopg.Connection = Depends(db)) -> AgentStatus:
    row = con.execute(
        f"SELECT {AGENT_STATUS_COLUMNS} FROM agents WHERE id = %s", (agent_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "agent not found")
    return AgentStatus(**row)


@app.put("/agents/{agent_id}/status")
def set_agent_status(
    agent_id: int, payload: AgentStatusIn, con: psycopg.Connection = Depends(db)
) -> AgentStatus:
    """Going unavailable stops new claims. Tickets already in_chat stay put --
    an agent who has the customer on the line finishes the conversation."""
    row = con.execute(
        f"UPDATE agents SET status = %s::agent_status WHERE id = %s"
        f" RETURNING {AGENT_STATUS_COLUMNS}",
        (payload.status, agent_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "agent not found")
    return AgentStatus(**row)
