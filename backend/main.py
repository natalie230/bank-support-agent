from datetime import datetime
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from db import claim, connect, waiting

app = FastAPI(title="bank-support-agent")


def db():
    with connect() as con:
        yield con


class TicketIn(BaseModel):
    customer_id: int
    language_ids: list[int] = Field(min_length=1)
    skill_ids: list[int] = Field(min_length=1)
    description: str = ""


class Ticket(BaseModel):
    id: int
    customer_id: int
    agent_id: int | None
    language_ids: list[int]
    status: Literal["open", "in_chat", "closed"]
    urgency: Literal["normal", "high"]
    description: str
    customer_name: str
    created_at: datetime
    skill_ids: list[int]


@app.exception_handler(psycopg.IntegrityError)
def integrity_error(request, exc: psycopg.IntegrityError):
    """Unknown customer/language/skill id is a bad request, not a 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc).splitlines()[0]})


def fetch(con: psycopg.Connection, ticket_id: int) -> Ticket:
    row = con.execute(
        """
        SELECT t.*, c.name AS customer_name,
               (SELECT COALESCE(array_agg(skill_id), '{}'::bigint[])
                FROM ticket_skills WHERE ticket_id = t.id) AS skill_ids,
               (SELECT COALESCE(array_agg(language_id), '{}'::bigint[])
                FROM ticket_languages WHERE ticket_id = t.id) AS language_ids
        FROM tickets t
        JOIN customers c ON c.id = t.customer_id
        WHERE t.id = %s
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
            """
            INSERT INTO tickets (customer_id, urgency, description)
            SELECT %(customer)s,
                   -- rule-based: one urgent skill makes the whole ticket urgent.
                   -- Not taken as input, or every customer would be urgent.
                   CASE WHEN bool_or(urgent) THEN 'high' ELSE 'normal' END::urgency_level,
                   %(description)s
            FROM skills WHERE id = ANY(%(skills)s::bigint[])
            RETURNING id
            """,
            {"customer": payload.customer_id, "description": payload.description,
             "skills": payload.skill_ids},
        ).fetchone()["id"]
        con.execute(
            "INSERT INTO ticket_skills SELECT %s, unnest(%s::bigint[])",
            (ticket_id, payload.skill_ids),
        )
        con.execute(
            "INSERT INTO ticket_languages SELECT %s, unnest(%s::bigint[])",
            (ticket_id, payload.language_ids),
        )
    return fetch(con, ticket_id)


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, con: psycopg.Connection = Depends(db)) -> Ticket:
    return fetch(con, ticket_id)


class MessageIn(BaseModel):
    sender: Literal["customer", "agent"]
    body: str = Field(min_length=1, max_length=4000)


class Message(BaseModel):
    id: int
    ticket_id: int
    sender: Literal["customer", "agent"]
    body: str
    created_at: datetime


@app.get("/tickets/{ticket_id}/messages")
def list_messages(ticket_id: int, con: psycopg.Connection = Depends(db)) -> list[Message]:
    """The chat so far, oldest first."""
    fetch(con, ticket_id)  # 404 if there is no such ticket
    # ponytail: the whole transcript on every poll. Add ?after=<id> when chats get long.
    rows = con.execute(
        "SELECT * FROM messages WHERE ticket_id = %s ORDER BY id", (ticket_id,)
    ).fetchall()
    return [Message(**r) for r in rows]


@app.post("/tickets/{ticket_id}/messages", status_code=201)
def post_message(
    ticket_id: int, payload: MessageIn, con: psycopg.Connection = Depends(db)
) -> Message:
    """Say something on a ticket."""
    if fetch(con, ticket_id).status == "closed":
        raise HTTPException(409, "ticket is closed")
    row = con.execute(
        "INSERT INTO messages (ticket_id, sender, body)"
        " VALUES (%s, %s::sender_kind, %s) RETURNING *",
        (ticket_id, payload.sender, payload.body),
    ).fetchone()
    return Message(**row)


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
    name: str
    status: Literal["available", "unavailable"]
    capacity: int
    active_ticket_ids: list[int]


AGENT_STATUS_COLUMNS = """
  id, name, status, capacity,
  -- which tickets, not how many: a console reloaded mid-chat has to find its
  -- way back to the ticket it is holding, and count alone cannot do that.
  COALESCE((SELECT array_agg(t.id ORDER BY t.claimed_at) FROM tickets t
            WHERE t.agent_id = agents.id AND t.status = 'in_chat'),
           '{}'::bigint[]) AS active_ticket_ids
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


class Named(BaseModel):
    id: int
    name: str


@app.get("/skills")
def list_skills(con: psycopg.Connection = Depends(db)) -> list[Named]:
    return [Named(**r) for r in
            con.execute("SELECT id, name FROM skills ORDER BY name").fetchall()]


@app.get("/languages")
def list_languages(con: psycopg.Connection = Depends(db)) -> list[Named]:
    return [Named(**r) for r in
            con.execute("SELECT id, name FROM languages ORDER BY name").fetchall()]


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr


class Customer(BaseModel):
    id: int
    name: str
    email: str


@app.post("/customers", status_code=201)
def create_customer(payload: CustomerIn, con: psycopg.Connection = Depends(db)) -> Customer:
    """One customer per email. Coming back with the same address is a lookup,
    not a duplicate -- the name they give this time wins."""
    row = con.execute(
        "INSERT INTO customers (name, email) VALUES (%s, %s)"
        " ON CONFLICT (lower(email)) DO UPDATE SET name = EXCLUDED.name"
        " RETURNING id, name, email",
        (payload.name, payload.email),
    ).fetchone()
    return Customer(**row)


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    skill_ids: list[int] = Field(min_length=1)
    language_ids: list[int] = Field(min_length=1)
    capacity: int = Field(default=1, ge=1)


@app.post("/agents", status_code=201)
def create_agent(payload: AgentIn, con: psycopg.Connection = Depends(db)) -> AgentStatus:
    """Starts unavailable -- a new hire is on the roster, not on the phones."""
    with con.transaction():
        agent_id = con.execute(
            "INSERT INTO agents (name, email, capacity) VALUES (%s, %s, %s) RETURNING id",
            (payload.name, payload.email, payload.capacity),
        ).fetchone()["id"]
        con.execute("INSERT INTO agent_skills SELECT %s, unnest(%s::bigint[])",
                    (agent_id, payload.skill_ids))
        con.execute("INSERT INTO agent_languages SELECT %s, unnest(%s::bigint[])",
                    (agent_id, payload.language_ids))
    return get_agent_status(agent_id, con)


@app.get("/agents")
def list_agents(con: psycopg.Connection = Depends(db)) -> list[AgentStatus]:
    return [AgentStatus(**r) for r in
            con.execute(f"SELECT {AGENT_STATUS_COLUMNS} FROM agents ORDER BY name").fetchall()]
