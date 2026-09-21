-- TEMPLATE ONLY. Run interactively on DB01 PostgreSQL 16 (port 5432) as postgres.
-- Do not put the production password in this file.

CREATE ROLE chopdapujan_app LOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\password chopdapujan_app

CREATE DATABASE chopdapujan_prod OWNER chopdapujan_app;
REVOKE ALL ON DATABASE chopdapujan_prod FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE chopdapujan_prod TO chopdapujan_app;

\du chopdapujan_app
\l+ chopdapujan_prod
