-- Reference data plus a starter roster, so a fresh schema is usable immediately.
-- Re-runnable: every insert ignores rows that already exist.

INSERT INTO skills (name) VALUES ('fraud'), ('mortgage'), ('cards'), ('accounts')
ON CONFLICT DO NOTHING;

INSERT INTO languages (name) VALUES ('English'), ('Mandarin'), ('Malay'), ('Tamil')
ON CONFLICT DO NOTHING;

INSERT INTO agents (name, email, capacity) VALUES
  ('Priya',  'priya@bank.example',  1),
  ('Wei',    'wei@bank.example',    2),
  ('Aisyah', 'aisyah@bank.example', 1)
ON CONFLICT DO NOTHING;

INSERT INTO agent_skills (agent_id, skill_id)
SELECT a.id, s.id FROM agents a, skills s
WHERE (a.email, s.name) IN (VALUES
  ('priya@bank.example',  'fraud'),
  ('priya@bank.example',  'cards'),
  ('wei@bank.example',    'mortgage'),
  ('wei@bank.example',    'accounts'),
  ('aisyah@bank.example', 'fraud'),
  ('aisyah@bank.example', 'mortgage'),
  ('aisyah@bank.example', 'cards'),
  ('aisyah@bank.example', 'accounts'))
ON CONFLICT DO NOTHING;

INSERT INTO agent_languages (agent_id, language_id)
SELECT a.id, l.id FROM agents a, languages l
WHERE (a.email, l.name) IN (VALUES
  ('priya@bank.example',  'English'),
  ('priya@bank.example',  'Tamil'),
  ('wei@bank.example',    'English'),
  ('wei@bank.example',    'Mandarin'),
  ('aisyah@bank.example', 'English'),
  ('aisyah@bank.example', 'Malay'))
ON CONFLICT DO NOTHING;
