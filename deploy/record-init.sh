# The initialisation step of the record database (RECORD-PROTOCOL.md sections 11.2 and 15). The
# entrypoint of the postgres image sources this file once, on an empty data directory, and runs it
# as the superuser of the server, with its own `set -e`: it creates the owner role and the writer
# role from the passwords given as secrets, each with LOGIN and no other attribute, no membership
# and no right, and the database `record` owned by the owner role. Every right of the writer comes
# from `python -m agent_core.record migrate`.

psql --no-psqlrc -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_password="$(cat /run/secrets/record_owner_password)" \
  -v writer_password="$(cat /run/secrets/record_writer_password)" <<'SQL'
CREATE ROLE record_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'owner_password';
CREATE ROLE record_writer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'writer_password';
CREATE DATABASE record OWNER record_owner;
SQL
