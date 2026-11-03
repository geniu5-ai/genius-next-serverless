#!/usr/bin/env bash
# Cofre do Standby: tabela standby_vault.secrets no Neon (valores cifrados FORA do banco).
#
#   scripts/vault.sh list              nomes, origem e data (nunca valores)
#   scripts/vault.sh get NOME          imprime o valor decifrado (use em $(...), nao em log)
#   printf '%s' "$VALOR" | scripts/vault.sh put NOME [ORIGEM]
#
# Ambiente:
#   STANDBY_VAULT_KEY   chave mestra (obrigatoria; fica so no ambiente da sessao)
#   DATABASE_URL        URL do Neon; se ausente, le do Render com RENDER_API_KEY
# Requer psql 15+ (\getenv) e openssl 3. O valor nunca vai em argumento de processo.
set -euo pipefail

: "${STANDBY_VAULT_KEY:?defina STANDBY_VAULT_KEY (chave mestra do cofre)}"
export STANDBY_VAULT_KEY
ENC_ARGS=(-aes-256-cbc -pbkdf2 -iter 200000 -pass env:STANDBY_VAULT_KEY -base64 -A)
NOME_RE='^[A-Za-z0-9_]+$'

db_url() {
  if [ -n "${DATABASE_URL:-}" ]; then printf '%s' "$DATABASE_URL"; return; fi
  : "${RENDER_API_KEY:?defina DATABASE_URL ou RENDER_API_KEY}"
  local srv="${STANDBY_RENDER_SERVICE:-srv-dbjvbvtg1s2s738rr2t0}"
  curl -sS -m 30 -H "Authorization: Bearer $RENDER_API_KEY" \
    "https://api.render.com/v1/services/$srv/env-vars?limit=100" |
    python3 -c "import sys,json;print(next(x['envVar']['value'] for x in json.load(sys.stdin) if x['envVar']['key']=='DATABASE_URL'))"
}

cmd="${1:-}"
case "$cmd" in
  list)
    psql "$(db_url)" -X -A -F' | ' -c "select name, source, updated_at::timestamp(0) from standby_vault.secrets order by name"
    ;;
  get)
    nome="${2:?uso: vault.sh get NOME}"; [[ "$nome" =~ $NOME_RE ]] || { echo "nome invalido" >&2; exit 2; }
    psql "$(db_url)" -X -A -t -c "select value_enc from standby_vault.secrets where name='$nome'" |
      openssl enc -d "${ENC_ARGS[@]}"
    ;;
  put)
    nome="${2:?uso: vault.sh put NOME [ORIGEM] (valor na stdin)}"; [[ "$nome" =~ $NOME_RE ]] || { echo "nome invalido" >&2; exit 2; }
    origem="${3:-manual}"
    valor="$(cat)"; [ -n "$valor" ] || { echo "valor vazio" >&2; exit 2; }
    ENC="$(printf '%s' "$valor" | openssl enc "${ENC_ARGS[@]}" -salt)" NAME="$nome" SRC="$origem" \
    psql "$(db_url)" -X -q -v ON_ERROR_STOP=1 <<'SQL'
\getenv n NAME
\getenv s SRC
\getenv e ENC
INSERT INTO standby_vault.secrets(name, value_enc, source) VALUES (:'n', :'e', :'s')
ON CONFLICT (name) DO UPDATE SET value_enc = EXCLUDED.value_enc, source = EXCLUDED.source, updated_at = now();
SQL
    echo "gravado: $nome" >&2
    ;;
  *) echo "uso: vault.sh list | get NOME | put NOME [ORIGEM]" >&2; exit 2 ;;
esac
