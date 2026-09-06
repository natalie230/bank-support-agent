# bank-support-agent

Routing for a bank's support desk. Customers raise tickets over chat, tickets
wait in a Postgres table, and free agents **claim** from it — nothing is pushed
to anyone. Once claimed, customer and agent chat on the ticket until the agent
closes it.

A ticket goes `open → in_chat → closed`, with one way back: a claim left
hanging too long returns to `open` so the agent's seat is not lost with it.
An agent claims a ticket only if they speak one of the customer's languages and cover
enough of its required skills — unless the ticket has waited past its
threshold, at which point any available agent will do. Urgency is rule-based:
a ticket touching an urgent skill (fraud, in the seed data) ranks ahead, and
the customer does not get to set it.

## Running it with Docker

One image runs both servers; `docker compose` adds Postgres.

```bash
docker compose up --build
```

Frontend at <http://localhost:8501>, API at <http://localhost:8000> (`/docs`).
Postgres loads `backend/schema.sql` and `backend/seed.sql` on its first boot
only — `docker compose down -v` wipes it for a fresh start. Override the
password and the routing knobs with a `.env` next to the compose file (see
`.env.example`).

On a single host such as one EC2 instance: install Docker, copy the repo (or
the built image plus `docker-compose.yml` and the two SQL files),
`docker compose up -d`, and open ports 8501 and 8000 in the security group.

## Running it locally

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), and a local Postgres.

```bash
uv sync
cp .env.example .env          # then fill in your Postgres password
createdb bank_support && createdb bank_support_test
uv run python backend/db.py   # DESTRUCTIVE: rebuilds the schema, loads seed data
uv run fastapi dev backend/main.py
uv run streamlit run frontend/app.py    # in a second terminal
```

Then open <http://localhost:8501>. Two pages: **Support chat** (the customer:
say who you are, describe the problem, wait, chat) and **Agent desk** (pick a
seeded agent, go available, claim, chat, close). Seed data ships three agents —
Priya, Wei and Aisyah — with different skills, languages and capacities, so the
matching rules are visible immediately. The frontend finds the API through
`BACKEND_URL` (default `http://localhost:8000`).

Interactive API docs are at <http://localhost:8000/docs>.

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
| `GET/POST /tickets/{id}/messages` | the chat on a ticket; 409 once closed |
| `GET /queue` | the waiting room, ranked as `claim` would take from it |
| `POST /agents` · `GET /agents` | roster |
| `GET/PUT /agents/{id}/status` | go available or unavailable |
| `POST /agents/{id}/claim` | take the next ticket, or `null` — poll again |
| `PUT /tickets/{id}/close` | finish a chat, freeing the agent's capacity |

## Layout

```
backend/main.py     FastAPI routes
backend/db.py       claim + release SQL, and the schema rebuild entry point
backend/schema.sql  tables; backend/seed.sql  reference data and roster
frontend/app.py     Streamlit console: customer chat and agent desk
Dockerfile          one image for both servers; docker-compose.yml adds Postgres
```

Tunable via `.env`: `SKILL_THRESHOLD`, `WAIT_THRESHOLD_SECONDS`,
`CLAIM_EXPIRY_SECONDS`. Architecture decisions are recorded as ADRs under
`docs/adr/`, which is kept local rather than committed.

## Windows note

If `fastapi dev` dies with `UnicodeEncodeError: 'charmap' codec`, its traceback
renderer hit a non-UTF-8 console. Run with `PYTHONUTF8=1`, or use
`uv run uvicorn main:app --app-dir backend`.
