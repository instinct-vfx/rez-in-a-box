-- Read-only database role for Grafana. Run by the grafana-db-init service on
-- every `docker compose --profile dashboard up`; safe to run repeatedly.
--   psql -v reader=grafana_reader -v reader_password=secret -f reader-role.sql
--
-- Default privileges cover tables the consumer creates later (it runs as the
-- same role as this script). Partitions need no grant: queries go through
-- the parent table.

SELECT format('CREATE ROLE %I LOGIN', :'reader')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'reader') \gexec

ALTER ROLE :"reader" WITH LOGIN PASSWORD :'reader_password';
-- Dashboards run ad-hoc queries; don't let a slow one run forever.
ALTER ROLE :"reader" SET statement_timeout = '30s';

GRANT USAGE ON SCHEMA public TO :"reader";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"reader";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"reader";
