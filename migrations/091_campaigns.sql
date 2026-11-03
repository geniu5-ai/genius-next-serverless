-- Campanhas e promocoes sazonais (03/nov/2026).
--
-- Campanha deixa de ser entrada ``promo:*`` de genius_next.knowledge (texto com a
-- validade escrita em prosa, que ninguem retirava quando vencia) e vira dado com
-- validade: o codigo (app/context/campaigns.py) injeta no contexto do modelo, na
-- chave ``campanhas_vigentes``, so as linhas com ativa e vale_de <= hoje <= vale_ate
-- (datas INCLUSIVAS, hoje no fuso America/Fortaleza). O catalogo passa a ignorar
-- chaves ``promo:*``. Operacao: docs/runbooks/RUNBOOK-CAMPANHAS.md.
--
-- Idempotente (CREATE ... IF NOT EXISTS): rodar duas vezes nao muda nada.
CREATE TABLE IF NOT EXISTS genius_next.campaigns (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL,
    slug text NOT NULL,
    titulo text NOT NULL,
    -- O texto que o agente pode usar: preco, condicao e o que a campanha sobrepoe.
    conteudo text NOT NULL,
    vale_de date NOT NULL,
    -- Ultimo dia de validade, INCLUSIVE.
    vale_ate date NOT NULL,
    ativa boolean NOT NULL DEFAULT true,
    criado_por text,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT campaigns_tenant_slug_key UNIQUE (tenant_id, slug),
    CONSTRAINT campaigns_slug_check CHECK (slug ~ '^[a-z0-9][a-z0-9_]*$'),
    CONSTRAINT campaigns_titulo_check CHECK (length(titulo) >= 1 AND length(titulo) <= 200),
    CONSTRAINT campaigns_conteudo_check CHECK (length(conteudo) >= 1 AND length(conteudo) <= 2000),
    CONSTRAINT campaigns_datas_check CHECK (vale_de <= vale_ate)
);

CREATE INDEX IF NOT EXISTS campaigns_tenant_vale_ate_idx
    ON genius_next.campaigns (tenant_id, vale_ate);

COMMENT ON TABLE genius_next.campaigns IS 'Campanhas e promocoes com validade. Injetadas no contexto do modelo so enquanto vigentes (app/context/campaigns.py).';
COMMENT ON COLUMN genius_next.campaigns.vale_ate IS 'Ultimo dia de validade, INCLUSIVE, no fuso America/Fortaleza.';

-- O baseline nao tem GRANTs (o app e dono das tabelas). Se o app conectar com outro
-- papel que nao o que roda a migration, ele precisa de acesso a esta tabela; sem o
-- papel (banco de teste, por exemplo) este bloco nao faz nada.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'genius_next_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON genius_next.campaigns TO genius_next_app;
        GRANT USAGE, SELECT ON SEQUENCE genius_next.campaigns_id_seq TO genius_next_app;
    END IF;
END
$$;
