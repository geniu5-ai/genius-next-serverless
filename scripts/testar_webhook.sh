#!/usr/bin/env bash
# Simula uma mensagem de cliente no /webhook/evolution e mostra a resposta do app.
# Uso: HMAC_SECRET=... ./scripts/testar_webhook.sh <instancia> <numero_ficticio> "<texto>"
# A resposta do agente fica em genius_next.turns / genius_next.outbox (record_only: nada vai ao WhatsApp).
set -euo pipefail
: "${HMAC_SECRET:?defina HMAC_SECRET (o mesmo do Render)}"
URL="${URL:-https://genius-next-serverless.onrender.com/webhook/evolution}"
INSTANCIA="${1:?instancia}"; NUMERO="${2:?numero ficticio, ex.: 5500000000010}"; TEXTO="${3:?texto}"
BODY=$(python3 -c 'import json,sys,time
print(json.dumps({"event":"messages.upsert","instance":sys.argv[1],"data":{
  "key":{"remoteJid":sys.argv[2]+"@s.whatsapp.net","fromMe":False,"id":"TEST%d"%time.time_ns()},
  "pushName":"Teste","message":{"conversation":sys.argv[3]},"messageType":"conversation",
  "messageTimestamp":int(time.time())}}))' "$INSTANCIA" "$NUMERO" "$TEXTO")
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$HMAC_SECRET" | awk '{print $NF}')"
curl -sS -m 90 -w " -> HTTP %{http_code}\n" -X POST "$URL" \
  -H 'content-type: application/json' -H "X-Hub-Signature-256: $SIG" --data-binary "$BODY"
