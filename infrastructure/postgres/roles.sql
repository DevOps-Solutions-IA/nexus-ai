-- NXS tenant database role model (NXS-SEC-003). Idempotent; run as a superuser.
--
-- nexus_migration : owns schema objects, runs Alembic. NOT superuser, cannot bypass RLS.
-- nexus_runtime   : the application request runtime. NOT superuser, NOBYPASSRLS, no
--                   schema ownership, least DML (SELECT/INSERT/UPDATE granted per tenant
--                   table by the migration; never DELETE on protected tenant records).
--
-- Passwords here are LOCAL / TEST ONLY. Production supplies real secrets out of band.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_migration') THEN
    CREATE ROLE nexus_migration LOGIN PASSWORD 'local-migration-only'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_runtime') THEN
    CREATE ROLE nexus_runtime LOGIN PASSWORD 'local-runtime-only'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE nexus_local TO nexus_migration, nexus_runtime;

GRANT CREATE, USAGE ON SCHEMA public TO nexus_migration;
GRANT USAGE ON SCHEMA public TO nexus_runtime;
REVOKE CREATE ON SCHEMA public FROM nexus_runtime;

-- Tables the migration role creates later become usable by the runtime role.
ALTER DEFAULT PRIVILEGES FOR ROLE nexus_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO nexus_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE nexus_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO nexus_runtime;
