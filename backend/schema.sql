DROP SCHEMA public CASCADE;
CREATE SCHEMA public;

CREATE TYPE ticket_status AS ENUM ('open', 'in_chat', 'closed');
CREATE TYPE agent_status AS ENUM ('available', 'unavailable');
CREATE TYPE urgency_level AS ENUM ('normal', 'high');

CREATE TABLE customers (
  id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name  VARCHAR(100) NOT NULL,
  email VARCHAR(254) NOT NULL
);

CREATE UNIQUE INDEX customers_email ON customers (lower(email));

CREATE TABLE agents (
  id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name     VARCHAR(100) NOT NULL,
  email    VARCHAR(254) NOT NULL,
  status   agent_status NOT NULL DEFAULT 'unavailable',
  capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity > 0)
);

CREATE UNIQUE INDEX agents_email ON agents (lower(email));

CREATE TABLE skills (
  id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name   VARCHAR(100) NOT NULL UNIQUE,
  urgent BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE languages (
  id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE agent_skills (
  agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  skill_id BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  PRIMARY KEY (agent_id, skill_id)
);

CREATE TABLE agent_languages (
  agent_id    BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  language_id BIGINT NOT NULL REFERENCES languages(id) ON DELETE CASCADE,
  PRIMARY KEY (agent_id, language_id)
);

CREATE TABLE tickets (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id BIGINT NOT NULL REFERENCES customers(id),
  agent_id    BIGINT REFERENCES agents(id),
  status      ticket_status NOT NULL DEFAULT 'open',
  urgency     urgency_level NOT NULL DEFAULT 'normal',
  description TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at  TIMESTAMPTZ,
  CHECK (status <> 'open'    OR agent_id IS NULL),
  CHECK (status <> 'in_chat' OR agent_id IS NOT NULL),
  CHECK (status <> 'open'    OR claimed_at IS NULL),
  CHECK (status <> 'in_chat' OR claimed_at IS NOT NULL)
);

CREATE INDEX tickets_open ON tickets (urgency DESC, created_at) WHERE status = 'open';

CREATE TABLE ticket_skills (
  ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  skill_id  BIGINT NOT NULL REFERENCES skills(id),
  PRIMARY KEY (ticket_id, skill_id)
);

-- what the customer can chat in; an agent needs to share any one of them
CREATE TABLE ticket_languages (
  ticket_id   BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  language_id BIGINT NOT NULL REFERENCES languages(id),
  PRIMARY KEY (ticket_id, language_id)
);

CREATE TYPE sender_kind AS ENUM ('customer', 'agent');

CREATE TABLE messages (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticket_id  BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  sender     sender_kind NOT NULL,
  body       TEXT NOT NULL CHECK (body <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX messages_ticket ON messages (ticket_id, id);
