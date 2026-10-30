#!/bin/sh
# PostgreSQL's first start (docker-entrypoint-initdb.d): agent-server's two roles and schemas.
#
#   mesh_api   owns schema mesh: settings, accounts, the fleet, sessions and their records.
#   mesh_auth  owns schema mesh_auth (agent-auth's OAuth state), and may only read the
#              account tables in mesh (agent-server grants that itself when it migrates).
#
# Each gets a password from the environment: AI_PROXY_DB_API_PASSWORD and
# AI_PROXY_DB_AUTH_PASSWORD (letters and digits). For a PostgreSQL of your own, run the
# same statements as its administrator; docs/deployment.md has them.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE mesh_api LOGIN PASSWORD '${AI_PROXY_DB_API_PASSWORD}';
CREATE ROLE mesh_auth LOGIN PASSWORD '${AI_PROXY_DB_AUTH_PASSWORD}';
CREATE SCHEMA mesh AUTHORIZATION mesh_api;
CREATE SCHEMA mesh_auth AUTHORIZATION mesh_auth;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE "${POSTGRES_DB}" FROM PUBLIC;
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO mesh_api, mesh_auth;
SQL
