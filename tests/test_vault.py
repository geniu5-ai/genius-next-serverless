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


# --------------------------------------------------------------------------- #
# run, backup e registro
# --------------------------------------------------------------------------- #
def test_dividir_run_exige_separador_nomes_e_comando():
    v = _vault()
    assert v.dividir_run(["A", "B", "--", "env", "-0"]) == (["A", "B"], ["env", "-0"])
    for ruim in (["A", "B"], ["--", "env"], ["A", "--"], []):
        with pytest.raises(v.CofreError, match="uso: vault.py run"):
            v.dividir_run(ruim)
    with pytest.raises(v.CofreError, match="nome invalido"):
        v.dividir_run(["A;B", "--", "env"])


def test_run_poe_segredo_so_no_ambiente_do_filho_e_nao_imprime(monkeypatch, capfd):
    import os
    import sys

    v = _vault()
    monkeypatch.setattr(v, "valor", lambda nome: {"SEGREDO_X": "abc123"}[nome])
    codigo = v.executar(
        ["SEGREDO_X"],
        [sys.executable, "-c", "import os,sys; sys.exit(0 if os.environ['SEGREDO_X']=='abc123' else 3)"],
    )
    saida = capfd.readouterr()
    assert codigo == 0
    assert "abc123" not in saida.out + saida.err
    assert "SEGREDO_X" not in os.environ  # o processo pai nao foi contaminado


def test_run_devolve_codigo_do_comando_e_erro_claro_se_nao_existe(monkeypatch):
    import sys

    v = _vault()
    monkeypatch.setattr(v, "valor", lambda nome: "x")
    assert v.executar(["A"], [sys.executable, "-c", "raise SystemExit(7)"]) == 7
    with pytest.raises(v.CofreError, match="comando nao encontrado"):
        v.executar(["A"], ["comando-que-nao-existe-xyz"])


def _itens(v):
    return [
        {"name": n, "value_enc": v.cifrar(MESTRA, n, f"valor-{n}"), "fmt": "aead1", "source": "teste",
         "updated_at": "2026-11-03 00:00:00+00"}
        for n in ("B_DOIS", "A_UM")
    ]


def test_backup_ida_e_volta_ordena_e_confere_a_chave():
    v = _vault()
    doc = v.montar_export(_itens(v))
    assert [i["name"] for i in doc["itens"]] == ["A_UM", "B_DOIS"]
    assert "valor-A_UM" not in str(doc)  # so texto cifrado
    assert len(v.validar_export(doc, MESTRA)) == 2
    with pytest.raises(v.CofreError, match="nao decifra"):
        v.validar_export(doc, "cd" * 32)  # chave errada: nao deixa importar


def test_backup_alterado_ou_de_outra_versao_e_recusado():
    v = _vault()
    doc = v.montar_export(_itens(v))
    doc["itens"][0]["source"] = "outra"
    with pytest.raises(v.CofreError, match="checksum"):
        v.validar_export(doc)
    with pytest.raises(v.CofreError, match="desconhecido"):
        v.validar_export({"versao": 99, "itens": []})
    antigo = v.montar_export([{**_itens(v)[0], "fmt": "cbc"}])
    with pytest.raises(v.CofreError, match="nao suportado"):
        v.validar_export(antigo)


def test_ator_do_log_tem_limite_e_prefixo(monkeypatch):
    v = _vault()
    monkeypatch.setenv("STANDBY_VAULT_ACTOR", "x" * 200)
    ator = v._ator()
    assert ator.startswith("vault.py:") and len(ator) == 63


def test_comandos_novos_sem_ambiente_saem_com_codigo_2(monkeypatch):
    v = _vault()
    for nome in ("STANDBY_VAULT_URL", "DATABASE_URL", "STANDBY_VAULT_KEY"):
        monkeypatch.delenv(nome, raising=False)
    for argv in (["history", "A"], ["restore", "A", "1"], ["log"], ["delete", "A"],
                 ["export", "/tmp/nao-vai-existir-xyz.json"], ["import", "/tmp/nao-existe.json"]):
        try:
            codigo = v.main(argv)
        except FileNotFoundError:
            codigo = 2  # import le o arquivo antes: ausente tambem e erro de uso
        assert codigo == 2, argv
    assert v.main(["restore", "A", "x"]) == 2  # ID nao numerico
    assert v.main(["run", "A"]) == 2  # sem --
