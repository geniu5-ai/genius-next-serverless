-- Cofre do Standby: esquema, funcoes e permissoes. Idempotente; rodar como dono do banco.
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/vault_schema.sql
--
-- A role vault_rw (criada por SQL, nunca pela API do Neon: TRAPS #7) NAO tem acesso as tabelas.
-- So executa as funcoes abaixo, que sao SECURITY DEFINER, tem search_path fixo e registram cada
-- acesso. Tabelas de historico e de log sao somente de insercao (gatilhos recusam UPDATE, DELETE
-- e TRUNCATE; o dono do banco ainda pode desligar o gatilho: o registro e rastro, nao cofre-forte).

CREATE SCHEMA IF NOT EXISTS standby_vault;
REVOKE ALL ON SCHEMA standby_vault FROM PUBLIC;

CREATE TABLE IF NOT EXISTS standby_vault.secrets (
  name       text PRIMARY KEY,
  value_enc  text NOT NULL,
  fmt        text NOT NULL DEFAULT 'aead1',
  source     text NOT NULL,
  note       text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE standby_vault.secrets ALTER COLUMN fmt SET DEFAULT 'aead1';

CREATE TABLE IF NOT EXISTS standby_vault.secret_versions (
  id        bigserial PRIMARY KEY,
  name      text NOT NULL,
  action    text NOT NULL CHECK (action IN ('put', 'delete', 'restore', 'import', 'migrate')),
  value_enc text,
  fmt       text,
  source    text,
  actor     text,
  db_user   text NOT NULL DEFAULT session_user,
  at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS secret_versions_name_id ON standby_vault.secret_versions (name, id);

CREATE TABLE IF NOT EXISTS standby_vault.access_log (
  id      bigserial PRIMARY KEY,
  name    text,
  action  text NOT NULL,
  actor   text,
  db_user text NOT NULL DEFAULT session_user,
  at      timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION standby_vault.refuse_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'standby_vault.% e somente de insercao', TG_TABLE_NAME;
END $$;

DROP TRIGGER IF EXISTS no_change ON standby_vault.secret_versions;
CREATE TRIGGER no_change BEFORE UPDATE OR DELETE ON standby_vault.secret_versions
  FOR EACH ROW EXECUTE FUNCTION standby_vault.refuse_change();
DROP TRIGGER IF EXISTS no_truncate ON standby_vault.secret_versions;
CREATE TRIGGER no_truncate BEFORE TRUNCATE ON standby_vault.secret_versions
  FOR EACH STATEMENT EXECUTE FUNCTION standby_vault.refuse_change();
DROP TRIGGER IF EXISTS no_change ON standby_vault.access_log;
CREATE TRIGGER no_change BEFORE UPDATE OR DELETE ON standby_vault.access_log
  FOR EACH ROW EXECUTE FUNCTION standby_vault.refuse_change();
DROP TRIGGER IF EXISTS no_truncate ON standby_vault.access_log;
CREATE TRIGGER no_truncate BEFORE TRUNCATE ON standby_vault.access_log
  FOR EACH STATEMENT EXECUTE FUNCTION standby_vault.refuse_change();

-- Rotulo de quem chamou: vem de application_name, que o vault.py preenche. Informativo, nao prova.
CREATE OR REPLACE FUNCTION standby_vault.vault_actor() RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
  SELECT coalesce(nullif(current_setting('application_name', true), ''), 'desconhecido')
$$;

CREATE OR REPLACE FUNCTION standby_vault.vault_get(p_name text)
RETURNS TABLE (value_enc text, fmt text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO standby_vault.access_log (name, action, actor)
    VALUES (p_name, 'get', standby_vault.vault_actor());
  RETURN QUERY SELECT s.value_enc, s.fmt FROM standby_vault.secrets s WHERE s.name = p_name;
END $$;

CREATE OR REPLACE FUNCTION standby_vault.vault_list()
RETURNS TABLE (name text, fmt text, source text, updated_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT s.name, s.fmt, s.source, s.updated_at FROM standby_vault.secrets s ORDER BY s.name
$$;

CREATE OR REPLACE FUNCTION standby_vault.vault_put(
  p_name text, p_value_enc text, p_fmt text, p_source text, p_action text DEFAULT 'put')
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF p_action NOT IN ('put', 'import') THEN
    RAISE EXCEPTION 'acao invalida: %', p_action;
  END IF;
  INSERT INTO standby_vault.secrets (name, value_enc, fmt, source)
    VALUES (p_name, p_value_enc, p_fmt, p_source)
    ON CONFLICT (name) DO UPDATE
      SET value_enc = EXCLUDED.value_enc, fmt = EXCLUDED.fmt,
          source = EXCLUDED.source, updated_at = now();
  INSERT INTO standby_vault.secret_versions (name, action, value_enc, fmt, source, actor)
    VALUES (p_name, p_action, p_value_enc, p_fmt, p_source, standby_vault.vault_actor());
  INSERT INTO standby_vault.access_log (name, action, actor)
    VALUES (p_name, p_action, standby_vault.vault_actor());
END $$;

-- "Apagar" guarda a ultima versao no historico: da para restaurar.
CREATE OR REPLACE FUNCTION standby_vault.vault_delete(p_name text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE achou boolean;
BEGIN
  INSERT INTO standby_vault.secret_versions (name, action, value_enc, fmt, source, actor)
    SELECT s.name, 'delete', s.value_enc, s.fmt, s.source, standby_vault.vault_actor()
    FROM standby_vault.secrets s WHERE s.name = p_name;
  DELETE FROM standby_vault.secrets WHERE name = p_name;
  achou := FOUND;
  INSERT INTO standby_vault.access_log (name, action, actor)
    VALUES (p_name, 'delete', standby_vault.vault_actor());
  RETURN achou;
END $$;

CREATE OR REPLACE FUNCTION standby_vault.vault_history(p_name text)
RETURNS TABLE (id bigint, action text, fmt text, source text, actor text, db_user text, at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT v.id, v.action, v.fmt, v.source, v.actor, v.db_user, v.at
  FROM standby_vault.secret_versions v WHERE v.name = p_name ORDER BY v.id DESC
$$;

CREATE OR REPLACE FUNCTION standby_vault.vault_restore(p_name text, p_id bigint)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v standby_vault.secret_versions;
BEGIN
  SELECT * INTO v FROM standby_vault.secret_versions
    WHERE id = p_id AND name = p_name AND value_enc IS NOT NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'versao % de % nao existe ou nao tem conteudo', p_id, p_name;
  END IF;
  INSERT INTO standby_vault.secrets (name, value_enc, fmt, source)
    VALUES (v.name, v.value_enc, v.fmt, v.source)
    ON CONFLICT (name) DO UPDATE
      SET value_enc = EXCLUDED.value_enc, fmt = EXCLUDED.fmt,
          source = EXCLUDED.source, updated_at = now();
  INSERT INTO standby_vault.secret_versions (name, action, value_enc, fmt, source, actor)
    VALUES (v.name, 'restore', v.value_enc, v.fmt, v.source, standby_vault.vault_actor());
  INSERT INTO standby_vault.access_log (name, action, actor)
    VALUES (p_name, 'restore', standby_vault.vault_actor());
END $$;

CREATE OR REPLACE FUNCTION standby_vault.vault_export()
RETURNS TABLE (name text, value_enc text, fmt text, source text, updated_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO standby_vault.access_log (name, action, actor)
    VALUES (NULL, 'export', standby_vault.vault_actor());
  RETURN QUERY SELECT s.name, s.value_enc, s.fmt, s.source, s.updated_at
    FROM standby_vault.secrets s ORDER BY s.name;
END $$;

CREATE OR REPLACE FUNCTION standby_vault.vault_access_log(p_limit integer DEFAULT 50)
RETURNS TABLE (id bigint, name text, action text, actor text, db_user text, at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT l.id, l.name, l.action, l.actor, l.db_user, l.at
  FROM standby_vault.access_log l ORDER BY l.id DESC LIMIT least(greatest(p_limit, 1), 1000)
$$;

-- Historico inicial das linhas que ja existiam.
INSERT INTO standby_vault.secret_versions (name, action, value_enc, fmt, source, actor)
  SELECT s.name, 'migrate', s.value_enc, s.fmt, s.source, 'vault_schema.sql'
  FROM standby_vault.secrets s
  WHERE NOT EXISTS (SELECT 1 FROM standby_vault.secret_versions v WHERE v.name = s.name);

-- Permissoes: a role vault_rw so executa as funcoes.
REVOKE ALL ON ALL TABLES IN SCHEMA standby_vault FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA standby_vault FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA standby_vault FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vault_rw') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA standby_vault FROM vault_rw;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA standby_vault FROM vault_rw;
    GRANT USAGE ON SCHEMA standby_vault TO vault_rw;
    GRANT EXECUTE ON FUNCTION
      standby_vault.vault_get(text), standby_vault.vault_list(),
      standby_vault.vault_put(text, text, text, text, text), standby_vault.vault_delete(text),
      standby_vault.vault_history(text), standby_vault.vault_restore(text, bigint),
      standby_vault.vault_export(), standby_vault.vault_access_log(integer)
      TO vault_rw;
  END IF;
END $$;
