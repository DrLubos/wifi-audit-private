-- Read-only database role for Claude Code (exploration and tests/perf/measure.sh
-- over `ssh google`). Idempotent; run by the developer, as the superuser, from
-- server/ on the box:
--
--   docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < ops/create_ro_role.sql
--
-- No password: the role can log in only over the local socket inside the db
-- container (the image's pg_hba trusts it; TCP needs scram, which fails without
-- a password). Writes are impossible because the role owns nothing and is
-- granted SELECT only (pg_read_all_data); default_transaction_read_only is an
-- extra guard, not the boundary. PostgreSQL 15+ (GRANT ... ON PARAMETER).

SELECT 'CREATE ROLE claude_ro' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'claude_ro') \gexec

ALTER ROLE claude_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL CONNECTION LIMIT 3;
GRANT pg_read_all_data, pg_monitor TO claude_ro;
GRANT SET ON PARAMETER track_io_timing TO claude_ro;   -- EXPLAIN (ANALYZE, BUFFERS) I/O times
ALTER ROLE claude_ro SET default_transaction_read_only = on;
ALTER ROLE claude_ro SET statement_timeout = '120s';
ALTER ROLE claude_ro SET idle_in_transaction_session_timeout = '5min';
