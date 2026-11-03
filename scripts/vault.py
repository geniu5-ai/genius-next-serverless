#!/usr/bin/env python3
"""Cofre do Standby: segredos cifrados no Neon (schema ``standby_vault``).

    vault.py list                              nomes, formato, origem e data (nunca valores)
    vault.py get NOME                          imprime o valor decifrado (use em $(...), nao em log)
    printf '%s' "$V" | vault.py put NOME [ORIGEM]
    vault.py delete NOME                       apaga, mas guarda a ultima versao no historico
    vault.py history NOME                      versoes (id, acao, quem, quando; sem valores)
    vault.py restore NOME ID                   volta o NOME para a versao ID
    vault.py log [N]                           ultimos N acessos (quem leu o que e quando)
    vault.py run NOME [NOME...] -- COMANDO...  roda COMANDO com os segredos so no ambiente dele
    vault.py export ARQUIVO [--force]          backup so com texto cifrado, com checksum
    vault.py import ARQUIVO                    restaura um backup (confere a chave antes)
    NOVA=<chave> vault.py rotate               decifra tudo com a chave atual e regrava com NOVA

Ambiente:
  STANDBY_VAULT_KEY    chave mestra: 64 hexadecimais aleatorios (>= 32 caracteres)
  STANDBY_VAULT_URL    URL do Neon da role ``vault_rw`` (alternativa: DATABASE_URL do dono)
  STANDBY_VAULT_ACTOR  rotulo de quem chama, gravado no log (padrao: usuario@maquina)

Cifra: AES-256-GCM (autenticada). A chave de 256 bits sai da chave mestra por HKDF-SHA256 e o nome
do segredo entra como dado autenticado associado (AAD): alterar o texto cifrado, ou trocar o
conteudo de duas linhas de lugar, faz a decifragem falhar em vez de devolver lixo. A cifra
acontece aqui, fora do banco: o servidor nunca ve valor nem chave. Formato em ``value_enc``:
base64( 0x01 || nonce(12) || texto cifrado+tag ), com ``fmt = 'aead1'``.

Acesso ao banco: a role ``vault_rw`` so executa as funcoes ``standby_vault.vault_*``
(scripts/vault_schema.sql), que registram cada acesso. Limites: o dono do banco ainda le tudo
(cifrado) e pode desligar os gatilhos; linha apagada e recuperavel pelo historico, mas o historico
tambem e apagavel pelo dono. Detalhes em docs/OPERACAO.md.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FMT = "aead1"
_VERSAO = b"\x01"
_NOME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MIN_CHAVE = 32
_EXPORT_VERSAO = 1


class CofreError(Exception):
    """Erro de uso ou de decifragem (a mensagem nunca carrega valor nem chave)."""


# --------------------------------------------------------------------------- #
# Cifra
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Backup (somente texto cifrado)
# --------------------------------------------------------------------------- #
def _checksum(itens: list[dict]) -> str:
    canonico = json.dumps(itens, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode()).hexdigest()


def montar_export(itens: list[dict], *, agora: datetime | None = None) -> dict:
    itens = sorted(itens, key=lambda i: i["name"])
    return {
        "versao": _EXPORT_VERSAO,
        "gerado_em": (agora or datetime.now(UTC)).isoformat(timespec="seconds"),
        "itens": itens,
        "sha256": _checksum(itens),
    }


def validar_export(doc: dict, mestra: str | None = None) -> list[dict]:
    """Confere versao, checksum e (se houver chave) se cada item decifra. Devolve os itens."""
    if doc.get("versao") != _EXPORT_VERSAO or "itens" not in doc:
        raise CofreError("arquivo de backup desconhecido")
    if doc.get("sha256") != _checksum(doc["itens"]):
        raise CofreError("backup corrompido ou alterado (checksum nao confere)")
    for item in doc["itens"]:
        validar_nome(item["name"])
        if item.get("fmt") != FMT:
            raise CofreError(f"{item['name']}: formato '{item.get('fmt')}' nao suportado")
        if mestra is not None:
            decifrar(mestra, item["name"], item["value_enc"])
    return doc["itens"]


# --------------------------------------------------------------------------- #
# Banco
# --------------------------------------------------------------------------- #
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


def _ator() -> str:
    ator = os.environ.get("STANDBY_VAULT_ACTOR")
    if not ator:
        try:
            ator = f"{getpass.getuser()}@{socket.gethostname()}"
        except Exception:  # noqa: BLE001 - sem usuario no contêiner
            ator = "desconhecido"
    return ("vault.py:" + ator)[:63]


def _conectar():
    import psycopg

    return psycopg.connect(_url(), connect_timeout=30, application_name=_ator())


def _buscar(nome: str) -> tuple[str, str] | None:
    with _conectar() as conn:
        return conn.execute(
            "select value_enc, fmt from standby_vault.vault_get(%s)", (validar_nome(nome),)
        ).fetchone()


def valor(nome: str) -> str:
    """Valor decifrado de ``nome`` (registra o acesso no log do cofre)."""
    linha = _buscar(nome)
    if linha is None:
        raise CofreError(f"{nome}: nao existe")
    if linha[1] != FMT:
        raise CofreError(f"{nome}: formato '{linha[1]}' antigo; regrave com 'put'")
    return decifrar(_mestra(), nome, linha[0])


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def listar() -> int:
    with _conectar() as conn:
        linhas = conn.execute(
            "select name, fmt, source, updated_at::timestamp(0) from standby_vault.vault_list()"
        ).fetchall()
    for nome, fmt, origem, quando in linhas:
        print(f"{nome} | {fmt} | {origem} | {quando}")
    return 0


def obter(nome: str) -> int:
    sys.stdout.write(valor(nome))
    return 0


def gravar(nome: str, origem: str, texto: str) -> int:
    if not texto:
        raise CofreError("valor vazio")
    blob = cifrar(_mestra(), nome, texto)
    with _conectar() as conn:
        conn.execute(
            "select standby_vault.vault_put(%s, %s, %s, %s)", (nome, blob, FMT, origem)
        )
    print(f"gravado: {nome}", file=sys.stderr)
    return 0


def apagar(nome: str) -> int:
    with _conectar() as conn:
        achou = conn.execute(
            "select standby_vault.vault_delete(%s)", (validar_nome(nome),)
        ).fetchone()[0]
    print(f"apagado: {nome}" if achou else f"{nome}: nao existia", file=sys.stderr)
    return 0


def historico(nome: str) -> int:
    with _conectar() as conn:
        linhas = conn.execute(
            "select id, action, fmt, source, actor, at::timestamp(0) from standby_vault.vault_history(%s)",
            (validar_nome(nome),),
        ).fetchall()
    for vid, acao, fmt, origem, ator, quando in linhas:
        print(f"{vid} | {acao} | {fmt} | {origem} | {ator} | {quando}")
    return 0


def restaurar(nome: str, versao: str) -> int:
    if not versao.isdigit():
        raise CofreError("ID de versao invalido")
    with _conectar() as conn:
        conn.execute("select standby_vault.vault_restore(%s, %s)", (validar_nome(nome), int(versao)))
    print(f"restaurado: {nome} <- versao {versao}", file=sys.stderr)
    return 0


def registro(limite: int) -> int:
    with _conectar() as conn:
        linhas = conn.execute(
            "select id, at::timestamp(0), action, coalesce(name, '-'), actor, db_user "
            "from standby_vault.vault_access_log(%s)",
            (limite,),
        ).fetchall()
    for lid, quando, acao, nome, ator, role in linhas:
        print(f"{lid} | {quando} | {acao} | {nome} | {ator} | {role}")
    return 0


def dividir_run(args: list[str]) -> tuple[list[str], list[str]]:
    """``NOME... -- COMANDO...`` -> (nomes, comando)."""
    if "--" not in args:
        raise CofreError("uso: vault.py run NOME [NOME...] -- COMANDO [ARGS...]")
    i = args.index("--")
    nomes, comando = args[:i], args[i + 1 :]
    if not nomes or not comando:
        raise CofreError("uso: vault.py run NOME [NOME...] -- COMANDO [ARGS...]")
    return [validar_nome(n) for n in nomes], comando


def executar(nomes: list[str], comando: list[str]) -> int:
    """Roda ``comando`` com os segredos so no ambiente dele; nada e impresso."""
    ambiente = dict(os.environ)
    for nome in nomes:
        ambiente[nome] = valor(nome)
    try:
        return subprocess.run(comando, env=ambiente, check=False).returncode
    except FileNotFoundError as exc:
        raise CofreError(f"comando nao encontrado: {comando[0]}") from exc


def exportar(arquivo: str, forcar: bool) -> int:
    destino = Path(arquivo)
    if destino.exists() and not forcar:
        raise CofreError(f"{arquivo} ja existe; use --force para sobrescrever")
    with _conectar() as conn:
        linhas = conn.execute(
            "select name, value_enc, fmt, source, updated_at::text from standby_vault.vault_export()"
        ).fetchall()
    itens = [
        {"name": n, "value_enc": v, "fmt": f, "source": s, "updated_at": u}
        for n, v, f, s, u in linhas
    ]
    doc = montar_export(itens)
    validar_export(doc, _mestra())  # nao grava backup que a chave atual nao abre
    destino.touch(mode=0o600, exist_ok=True)
    destino.chmod(0o600)
    destino.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"exportado: {len(itens)} entradas cifradas em {arquivo} (sha256 {doc['sha256'][:12]})", file=sys.stderr)
    return 0


def importar(arquivo: str) -> int:
    doc = json.loads(Path(arquivo).read_text())
    itens = validar_export(doc, _mestra())
    with _conectar() as conn:
        for item in itens:
            conn.execute(
                "select standby_vault.vault_put(%s, %s, %s, %s, 'import')",
                (item["name"], item["value_enc"], item["fmt"], item["source"]),
            )
    print(f"importado: {len(itens)} entradas", file=sys.stderr)
    return 0


def rotacionar(nova: str) -> int:
    """Decifra tudo com a chave atual e regrava com ``nova``, numa transacao."""
    _chave(nova)  # valida o tamanho antes de tocar no banco
    atual = _mestra()
    with _conectar() as conn:
        linhas = conn.execute(
            "select name, value_enc, fmt, source from standby_vault.vault_export()"
        ).fetchall()
        for nome, blob, fmt, origem in linhas:
            if fmt != FMT:
                raise CofreError(f"{nome}: formato '{fmt}' antigo; migre antes de rotacionar")
            conn.execute(
                "select standby_vault.vault_put(%s, %s, %s, %s)",
                (nome, cifrar(nova, nome, decifrar(atual, nome, blob)), FMT, origem),
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
            case ["history", nome]:
                return historico(nome)
            case ["restore", nome, versao]:
                return restaurar(nome, versao)
            case ["log"]:
                return registro(50)
            case ["log", n] if n.isdigit():
                return registro(int(n))
            case ["run", *resto]:
                return executar(*dividir_run(resto))
            case ["export", arquivo, *flags] if set(flags) <= {"--force"}:
                return exportar(arquivo, "--force" in flags)
            case ["import", arquivo]:
                return importar(arquivo)
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
