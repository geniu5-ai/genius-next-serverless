#!/usr/bin/env bash
# Backup do repositorio em um git bundle (todas as refs e todo o historico) e, opcionalmente, no R2.
#
#   scripts/backup_repo.sh                  cria o bundle, verifica e testa a restauracao (local)
#   scripts/backup_repo.sh --subir          idem e sobe para o R2 (precisa das variaveis R2_*)
#   scripts/backup_repo.sh --so-subir ARQ   sobe um bundle que ja existe (nao cria nada)
#
# Ambiente para subir: R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.
# Destino: <bucket>/Genius-Standby/standby-<data UTC>-<sha7 da main>.bundle (+ .sha256), na raiz do bucket.
# Outra pasta: R2_PREFIX=nome (padrao Genius-Standby).
# Nunca sobrescreve: se o nome ja existe, para com codigo 4. Confere o objeto por download e hash.
# O bundle NAO e cifrado: contem todo o historico (inclua so em bucket privado).
set -euo pipefail

modo="local"; arq=""
case "${1:-}" in
  "") ;;
  --subir) modo="subir" ;;
  --so-subir) modo="so-subir"; arq="${2:?uso: backup_repo.sh --so-subir ARQUIVO.bundle}" ;;
  *) echo "uso: backup_repo.sh [--subir | --so-subir ARQUIVO]" >&2; exit 2 ;;
esac

D="$(mktemp -d)"; trap 'rm -rf "$D"' EXIT

if [ "$modo" != "so-subir" ]; then
  if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    echo "clone raso: completando o historico (git fetch --unshallow)..." >&2
    git fetch -q --unshallow origin
  fi
  nome="standby-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=7 main).bundle"
  git bundle create "$D/$nome" --all >/dev/null 2>&1
  git bundle verify "$D/$nome" >/dev/null 2>&1 || { echo "bundle nao verifica" >&2; exit 3; }
  git clone -q "$D/$nome" "$D/teste" && git -C "$D/teste" fsck >/dev/null 2>&1 || { echo "restauracao de teste falhou" >&2; exit 3; }
  [ "$(git -C "$D/teste" ls-files | wc -l)" = "$(git ls-files | wc -l)" ] || echo "aviso: a arvore de trabalho atual tem arquivos diferentes dos commitados" >&2
  arq="$D/$nome"
  echo "bundle ok e restauravel: $nome ($(wc -c <"$arq") bytes, $(git rev-list --all --count) commits)" >&2
  if [ "$modo" = "local" ]; then
    [ -e "./$nome" ] || cp "$arq" "./$nome"; echo "salvo em ./$nome (guarde fora do repositorio; *.bundle nao e versionado)" >&2; exit 0
  fi
fi

for v in R2_ENDPOINT R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do : "${!v:?defina $v}"; done
nome="$(basename "$arq")"; sha="$(sha256sum "$arq" | cut -d' ' -f1)"
printf '%s  %s\n' "$sha" "$nome" >"$D/$nome.sha256"
ep="${R2_ENDPOINT%/}"; case "$ep" in */"$R2_BUCKET") ep="${ep%/"$R2_BUCKET"}";; esac
prefixo="${R2_PREFIX:-Genius-Standby}"
base="$ep/$R2_BUCKET/$prefixo"
s3() { curl -sS -m 120 --aws-sigv4 "aws:amz:auto:s3" --user "$R2_ACCESS_KEY_ID:$R2_SECRET_ACCESS_KEY" "$@"; }

for f in "$nome" "$nome.sha256"; do
  code="$(s3 -o /dev/null -w '%{http_code}' -I "$base/$f")"
  [ "$code" = 404 ] || { echo "$f ja existe ou erro (HTTP $code): nao sobrescrevo" >&2; exit 4; }
done
for f in "$nome" "$nome.sha256"; do
  src="$D/$f"; [ "$f" = "$nome" ] && src="$arq"
  h="$(sha256sum "$src" | cut -d' ' -f1)"
  code="$(s3 -o /dev/null -w '%{http_code}' -T "$src" -H "x-amz-content-sha256: $h" -H 'content-type: application/octet-stream' "$base/$f")"
  [ "$code" = 200 ] || { echo "PUT $f falhou (HTTP $code)" >&2; exit 5; }
  echo "enviado: $f" >&2
done
s3 -o "$D/volta" "$base/$nome"
[ "$(sha256sum "$D/volta" | cut -d' ' -f1)" = "$sha" ] || { echo "objeto no R2 diverge do local" >&2; exit 6; }
echo "conferido por download: sha256 $sha" >&2
echo "$prefixo/$nome"
