# bank-support-agent

Routing for a bank's support desk. Customers raise tickets, tickets wait in a
Postgres table, and free agents **claim** from it — nothing is pushed to anyone.

A ticket goes `open → in_chat → closed`, with one way back: a claim left
hanging too long returns to `open` so the agent's seat is not lost with it.
An agent claims a ticket only if they speak the customer's language and cover
enough of its required skills — unless the ticket has waited past its
threshold, at which point any available agent will do.

## Running it

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), and a local Postgres.

```bash
uv sync
cp .env.example .env          # then fill in your Postgres password
createdb bank_support && createdb bank_support_test
uv run python backend/db.py   # DESTRUCTIVE: rebuilds the schema, loads seed data
uv run fastapi dev backend/main.py
```

Then open <http://localhost:8000>. The console has two tabs: **Raise a ticket**
(the customer) and **Agent desk** (pick a seeded agent, go available, claim).
Seed data ships three agents — Priya, Wei and Aisyah — with different skills,
languages and capacities, so the matching rules are visible immediately.

Interactive API docs are at `/docs`.

## Tests

Both scripts rebuild the schema in `TEST_DATABASE_URL`, so point it somewhere
scratch. Plain asserts, no test runner.

```bash
uv run python backend/test_claim.py && uv run python backend/test_api.py
```

## API

| | |
|---|---|
| `POST /customers` | create or look up by email |
| `POST /tickets` · `GET /tickets/{id}` | raise a ticket, check on one |
| `GET /queue` | the waiting room, ranked as `claim` would take from it |
| `POST /agents` · `GET /agents` | roster |
| `GET/PUT /agents/{id}/status` | go available or unavailable |
| `POST /agents/{id}/claim` | take the next ticket, or `null` — poll again |
| `PUT /tickets/{id}/close` | finish a chat, freeing the agent's capacity |

## Layout

```
backend/main.py     FastAPI routes
backend/db.py       claim + release SQL, and the schema rebuild entry point
backend/index.html  the whole frontend, one file, no build step
backend/schema.sql  tables; backend/seed.sql  reference data and roster
```

Tunable via `.env`: `SKILL_THRESHOLD`, `WAIT_THRESHOLD_SECONDS`,
`CLAIM_EXPIRY_SECONDS`. Architecture decisions are recorded as ADRs under
`docs/adr/`, which is kept local rather than committed.

## Windows note

If `fastapi dev` dies with `UnicodeEncodeError: 'charmap' codec`, its traceback
renderer hit a non-UTF-8 console. Run with `PYTHONUTF8=1`, or use
`uv run uvicorn main:app --app-dir backend`.
