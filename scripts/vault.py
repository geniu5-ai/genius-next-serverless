#!/usr/bin/env python3
"""Cofre do Standby: segredos cifrados na tabela ``standby_vault.secrets`` do Neon.

    vault.py list                       nomes, formato, origem e data (nunca valores)
    vault.py get NOME                   imprime o valor decifrado (use em $(...), nao em log)
    printf '%s' "$V" | vault.py put NOME [ORIGEM]
    vault.py delete NOME
    NOVA=<chave> vault.py rotate        decifra tudo com a chave atual e regrava com NOVA

Ambiente:
  STANDBY_VAULT_KEY   chave mestra: 64 caracteres hexadecimais aleatorios (>= 32 caracteres)
  STANDBY_VAULT_URL   URL do Neon do role ``vault_rw`` (so enxerga o schema do cofre)
                      (alternativa: DATABASE_URL)

Cifra: AES-256-GCM (autenticada). A chave de 256 bits sai da chave mestra por HKDF-SHA256 e o nome
do segredo entra como dado autenticado associado (AAD): alterar o texto cifrado, ou trocar o
conteudo de duas linhas de lugar, faz a decifragem falhar em vez de devolver lixo. A cifra acontece
aqui, fora do banco: o servidor nunca ve valor nem chave. Formato gravado em ``value_enc``:
base64( 0x01 || nonce(12) || texto cifrado+tag ), com ``fmt = 'aead1'``.

Limites: nao detecta linha apagada ou voltada a uma versao antiga; a chave mestra e ponto unico de
falha. Detalhes em docs/OPERACAO.md.
"""

from __future__ import annotations

import base64
import os
import re
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FMT = "aead1"
_VERSAO = b"\x01"
_NOME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MIN_CHAVE = 32


class CofreError(Exception):
    """Erro de uso ou de decifragem (a mensagem nunca carrega valor nem chave)."""


def _chave(mestra: str) -> bytes:
    if len(mestra) < _MIN_CHAVE:
        raise CofreError("STANDBY_VAULT_KEY curta demais: use 64 hexadecimais aleatorios")
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"standby-vault-v1"
    ).derive(mestra.encode())


def _aad(nome: str) -> bytes:
    return b"standby-vault-v1:" + nome.encode()


def validar_nome(nome: str) -> str:
    if not _NOME_RE.match(nome):
        raise CofreError("nome invalido: use letras, numeros e _")
    return nome


def cifrar(mestra: str, nome: str, valor: str) -> str:
    nonce = os.urandom(12)
    corpo = AESGCM(_chave(mestra)).encrypt(nonce, valor.encode(), _aad(validar_nome(nome)))
    return base64.b64encode(_VERSAO + nonce + corpo).decode()


def decifrar(mestra: str, nome: str, blob: str) -> str:
    bruto = base64.b64decode(blob)
    if len(bruto) < 1 + 12 + 16 or bruto[:1] != _VERSAO:
        raise CofreError(f"{nome}: formato desconhecido")
    try:
        return AESGCM(_chave(mestra)).decrypt(
            bruto[1:13], bruto[13:], _aad(validar_nome(nome))
        ).decode()
    except InvalidTag as exc:
        raise CofreError(
            f"{nome}: nao decifra (chave errada, linha adulterada ou trocada de lugar)"
        ) from exc


def _url() -> str:
    url = os.environ.get("STANDBY_VAULT_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise CofreError("defina STANDBY_VAULT_URL (ou DATABASE_URL)")
    return url.strip().strip("\"'")


def _mestra() -> str:
    mestra = os.environ.get("STANDBY_VAULT_KEY", "").strip()
    if not mestra:
        raise CofreError("defina STANDBY_VAULT_KEY")
    return mestra


def _conectar():
    import psycopg

    return psycopg.connect(_url(), connect_timeout=30)


def listar() -> int:
    with _conectar() as conn:
        linhas = conn.execute(
            "select name, fmt, source, updated_at::timestamp(0) from standby_vault.secrets order by name"
        ).fetchall()
    for nome, fmt, origem, quando in linhas:
        print(f"{nome} | {fmt} | {origem} | {quando}")
    return 0


def obter(nome: str) -> int:
    validar_nome(nome)
    with _conectar() as conn:
        linha = conn.execute(
            "select value_enc, fmt from standby_vault.secrets where name = %s", (nome,)
        ).fetchone()
    if linha is None:
        raise CofreError(f"{nome}: nao existe")
    if linha[1] != FMT:
        raise CofreError(f"{nome}: formato '{linha[1]}' antigo; regrave com 'put'")
    sys.stdout.write(decifrar(_mestra(), nome, linha[0]))
    return 0


def gravar(nome: str, origem: str, valor: str) -> int:
    if not valor:
        raise CofreError("valor vazio")
    blob = cifrar(_mestra(), nome, valor)
    with _conectar() as conn:
        conn.execute(
            "insert into standby_vault.secrets(name, value_enc, fmt, source) values (%s, %s, %s, %s) "
            "on conflict (name) do update set value_enc = excluded.value_enc, fmt = excluded.fmt, "
            "source = excluded.source, updated_at = now()",
            (nome, blob, FMT, origem),
        )
    print(f"gravado: {nome}", file=sys.stderr)
    return 0


def apagar(nome: str) -> int:
    validar_nome(nome)
    with _conectar() as conn:
        conn.execute("delete from standby_vault.secrets where name = %s", (nome,))
    print(f"apagado: {nome}", file=sys.stderr)
    return 0


def rotacionar(nova: str) -> int:
    """Decifra tudo com a chave atual e regrava com ``nova``, numa transacao."""
    _chave(nova)  # valida o tamanho antes de tocar no banco
    atual = _mestra()
    with _conectar() as conn:
        linhas = conn.execute("select name, value_enc, fmt from standby_vault.secrets").fetchall()
        for nome, blob, fmt in linhas:
            if fmt != FMT:
                raise CofreError(f"{nome}: formato '{fmt}' antigo; migre antes de rotacionar")
            conn.execute(
                "update standby_vault.secrets set value_enc = %s, updated_at = now() where name = %s",
                (cifrar(nova, nome, decifrar(atual, nome, blob)), nome),
            )
    print(f"rotacionadas {len(linhas)} entradas; troque STANDBY_VAULT_KEY", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["list"]:
                return listar()
            case ["get", nome]:
                return obter(nome)
            case ["put", nome, *resto] if len(resto) <= 1:
                return gravar(validar_nome(nome), resto[0] if resto else "manual", sys.stdin.read())
            case ["delete", nome]:
                return apagar(nome)
            case ["rotate"]:
                return rotacionar(os.environ.get("NOVA", "").strip())
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except CofreError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
