"""Cofre: cifra autenticada (AES-256-GCM), AAD por nome e validacao de entrada."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

MESTRA = "ab" * 32  # 64 hexadecimais


def _vault():
    caminho = Path(__file__).resolve().parent.parent / "scripts" / "vault.py"
    spec = importlib.util.spec_from_file_location("vault", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_ida_e_volta_com_caracteres_especiais():
    v = _vault()
    valor = "postgresql://u:p@h/db?sslmode=require&x=çã\n\"'$"
    blob = v.cifrar(MESTRA, "NEON_API_KEY", valor)
    assert valor not in blob and "postgresql" not in blob
    assert v.decifrar(MESTRA, "NEON_API_KEY", blob) == valor


def test_nonce_aleatorio_cifra_igual_gera_blobs_diferentes():
    v = _vault()
    assert v.cifrar(MESTRA, "A", "x") != v.cifrar(MESTRA, "A", "x")


def test_chave_errada_nao_decifra():
    v = _vault()
    blob = v.cifrar(MESTRA, "A", "segredo")
    with pytest.raises(v.CofreError, match="nao decifra"):
        v.decifrar("cd" * 32, "A", blob)


def test_texto_cifrado_adulterado_e_detectado():
    v = _vault()
    bruto = bytearray(base64.b64decode(v.cifrar(MESTRA, "A", "segredo")))
    bruto[-1] ^= 0x01
    with pytest.raises(v.CofreError, match="nao decifra"):
        v.decifrar(MESTRA, "A", base64.b64encode(bytes(bruto)).decode())


def test_linha_trocada_de_lugar_e_detectada_pelo_aad():
    v = _vault()
    blob_a = v.cifrar(MESTRA, "A", "valor-de-a")
    with pytest.raises(v.CofreError, match="nao decifra"):
        v.decifrar(MESTRA, "B", blob_a)  # o conteudo de A gravado na linha B


def test_formato_desconhecido_e_chave_curta():
    v = _vault()
    with pytest.raises(v.CofreError, match="formato desconhecido"):
        v.decifrar(MESTRA, "A", base64.b64encode(b"\x09" + b"0" * 40).decode())
    with pytest.raises(v.CofreError, match="curta demais"):
        v.cifrar("curta", "A", "x")


@pytest.mark.parametrize("nome", ["", "A B", "A;drop", "a-b", "../x", "A'--"])
def test_nome_invalido_e_recusado(nome):
    v = _vault()
    with pytest.raises(v.CofreError, match="nome invalido"):
        v.validar_nome(nome)


def test_erro_nunca_carrega_valor_nem_chave():
    v = _vault()
    blob = v.cifrar(MESTRA, "A", "valor-super-secreto")
    with pytest.raises(v.CofreError) as exc:
        v.decifrar("cd" * 32, "A", blob)
    assert "valor-super-secreto" not in str(exc.value) and MESTRA not in str(exc.value)


def test_sem_ambiente_sai_com_codigo_2(monkeypatch):
    v = _vault()
    for nome in ("STANDBY_VAULT_URL", "DATABASE_URL", "STANDBY_VAULT_KEY"):
        monkeypatch.delenv(nome, raising=False)
    assert v.main(["list"]) == 2
    assert v.main([]) == 2
