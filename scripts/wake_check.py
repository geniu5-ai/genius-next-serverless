#!/usr/bin/env python3
"""Pergunta ao Neon se ha trabalho vencido e, so entao, acorda o servico no Render.

Roda no GitHub Actions (``.github/workflows/wake.yml``) a cada 20 min. Nao decide quem o
agente aborda: so mede se algo esta esperando (``app/wake.py``) e chama
``POST /v1/control/wake`` assinado, o que acorda o processo; ao subir, o Procrastinate
retoma sozinho jobs vencidos, travados e o ultimo tick de cada relogio.

Ambiente:
  WAKE_DATABASE_URL     usuario SOMENTE LEITURA do Neon (obrigatorio)
  WAKE_URL              ex.: https://<servico>.onrender.com (obrigatorio, sem barra final)
  CONTROL_HMAC_SECRET   segredo da assinatura de controle (obrigatorio para acordar)
  WAKE_TENANTS          lista separada por virgula; vazio = todos
  WAKE_LOOKAHEAD_S      padrao 300
  WAKE_FORCE            "1" acorda mesmo sem trabalho (teste)

Uso: wake_check.py [--check-only]   (so mede, nunca chama o servico)
Saida: 0 = ok (acordou ou nao precisava); 1 = precisava acordar e falhou; 2 = erro de uso/banco.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Carrega app/wake.py pelo caminho: `import app.wake` executaria app/__init__.py, que importa o
# app inteiro, e o Actions so tem o psycopg instalado.
_spec = importlib.util.spec_from_file_location(
    "wake", Path(__file__).resolve().parent.parent / "app" / "wake.py"
)
_wake = importlib.util.module_from_spec(_spec)
sys.modules["wake"] = _wake  # o @dataclass resolve o modulo por este nome
_spec.loader.exec_module(_wake)
WAKE_SQL, parse_tenants, state_from_row, wake_params = (
    _wake.WAKE_SQL, _wake.parse_tenants, _wake.state_from_row, _wake.wake_params,
)

TENTATIVAS = 4
TIMEOUT_S = 100  # o plano free leva ate ~60 s para acordar


def medir(database_url: str, *, tenants: list[str], lookahead_s: int):
    import psycopg

    with psycopg.connect(database_url, connect_timeout=30, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        row = conn.execute(
            WAKE_SQL, wake_params(tenants=tenants, lookahead_s=lookahead_s)
        ).fetchone()
    return state_from_row(row)


def assinar(secret: str, body: bytes, timestamp: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def acordar(base_url: str, secret: str, motivo: dict) -> dict:
    body = json.dumps({"reason": motivo}, separators=(",", ":")).encode()
    ultimo_erro = ""
    for tentativa in range(1, TENTATIVAS + 1):
        ts = str(int(time.time()))
        req = urllib.request.Request(
            f"{base_url}/v1/control/wake",
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "X-Control-Timestamp": ts,
                "X-Control-Signature-256": assinar(secret, body, ts),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return {"http": resp.status, "body": json.loads(resp.read() or b"{}")}
        except urllib.error.HTTPError as exc:
            ultimo_erro = f"HTTP {exc.code}"
            if exc.code in (401, 403, 422):
                break  # erro nosso (assinatura/corpo): repetir nao ajuda
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            ultimo_erro = type(exc).__name__
        print(f"tentativa {tentativa}/{TENTATIVAS} falhou ({ultimo_erro})", file=sys.stderr)
        time.sleep(15)
    raise RuntimeError(f"nao acordou: {ultimo_erro}")


def escrever_saida(**itens: object) -> None:
    caminho = os.environ.get("GITHUB_OUTPUT")
    if not caminho:
        return
    with open(caminho, "a", encoding="utf-8") as saida:
        saida.writelines(f"{chave}={valor}\n" for chave, valor in itens.items())


def main(argv: list[str]) -> int:
    check_only = "--check-only" in argv
    database_url = os.environ.get("WAKE_DATABASE_URL", "")
    if not database_url:
        print("defina WAKE_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        estado = medir(
            database_url,
            tenants=parse_tenants(os.environ.get("WAKE_TENANTS", "")),
            lookahead_s=int(os.environ.get("WAKE_LOOKAHEAD_S", "300")),
        )
    except Exception as exc:  # noqa: BLE001 - qualquer falha do banco vira codigo 2
        print(f"erro ao consultar o banco: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(estado.as_dict()))
    forcar = os.environ.get("WAKE_FORCE") == "1"
    precisa = estado.wake_needed or forcar
    escrever_saida(wake_needed=str(precisa).lower())
    if check_only or not precisa:
        print("nada vencido: o servico continua dormindo" if not precisa else "precisa acordar")
        return 0
    base_url = os.environ.get("WAKE_URL", "").rstrip("/")
    secret = os.environ.get("CONTROL_HMAC_SECRET", "")
    if not base_url or not secret:
        print("defina WAKE_URL e CONTROL_HMAC_SECRET", file=sys.stderr)
        return 2
    try:
        resposta = acordar(base_url, secret, estado.as_dict())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(resposta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
