"""Tool DocuSeal: gera procuração HTML e cria link de assinatura via Cloud API Pro."""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import date

import httpx

DOCUSEAL_CLOUD_API_KEY = os.getenv("DOCUSEAL_CLOUD_API_KEY", "")
DOCUSEAL_CLOUD_URL = "https://api.docuseal.com"
# A chave vem SEMPRE do ambiente (nunca no código: o repositório do Standby é público e o histórico guarda para sempre).
# Sem a variável, a consulta de CPF falha com 401 em vez de usar um valor padrão.
CPFHUB_API_KEY = os.getenv("CPFHUB_API_KEY", "")

DR_NOME   = os.getenv("DR_NOME",   "Dr. Cláudio Lopes")
DR_OAB    = os.getenv("DR_OAB",    "OAB/CE 12345")
DR_CIDADE = os.getenv("DR_CIDADE", "Guaraciaba do Norte/CE")

_MESES_PT = [
    "janeiro","fevereiro","março","abril","maio","junho",
    "julho","agosto","setembro","outubro","novembro","dezembro",
]

GENERO_REQUERIDO: dict[str, str] = {
    "gestante": "F",
}

OBJETOS: dict[str, str] = {
    "inss_beneficio": (
        "representá-lo(a) perante o INSS e demais órgãos competentes para requerer, "
        "acompanhar e praticar todos os atos necessários ao requerimento, concessão, "
        "revisão ou recurso de benefício previdenciário (aposentadoria, auxílio-doença, "
        "pensão por morte e demais benefícios do RGPS), podendo assinar documentos, "
        "interpor recursos, receber citações, transigir, desistir e substabelecer."
    ),
    "inss_rural": (
        "representá-lo(a) perante o INSS e demais órgãos competentes para requerer, "
        "acompanhar e praticar todos os atos necessários ao requerimento, concessão ou "
        "recurso de Aposentadoria Rural (por Idade ou Invalidez) e demais benefícios do "
        "segurado especial, podendo assinar documentos, interpor recursos, receber "
        "citações, transigir, desistir e substabelecer."
    ),
    "bpc_loas": (
        "representá-lo(a) perante o INSS, CRAS, CREAS e demais órgãos da Assistência "
        "Social para requerer, acompanhar e praticar todos os atos necessários ao "
        "requerimento, concessão, revisão ou recurso do Benefício de Prestação "
        "Continuada - BPC/LOAS (Lei n.º 8.742/93), podendo assinar documentos, "
        "interpor recursos, receber citações, transigir, desistir e substabelecer."
    ),
    "gestante": (
        "representá-lo(a) perante o INSS, empregador e demais órgãos competentes para "
        "requerer, acompanhar e praticar todos os atos necessários ao requerimento, "
        "concessão ou recurso de salário-maternidade e demais benefícios previdenciários "
        "vinculados à gestação e maternidade, podendo assinar documentos, interpor "
        "recursos, receber citações, transigir, desistir e substabelecer."
    ),
    "financiamento_invalidez": (
        "representá-lo(a) perante o INSS, instituições financeiras e seguradoras para "
        "requerer, acompanhar e praticar todos os atos necessários ao requerimento de "
        "seguro prestamista e quitação de financiamento por invalidez, podendo assinar "
        "documentos, interpor recursos, receber citações, transigir, desistir e substabelecer."
    ),
}


# ─── helpers ────────────────────────────────────────────────────────────────

def _data_hoje_pt() -> str:
    """Retorna data atual por extenso em português: '08 de setembro de 2026'."""
    hoje = date.today()
    return f"{hoje.day:02d} de {_MESES_PT[hoje.month - 1]} de {hoje.year}"


def nome_parece_valido(nome: str) -> bool:
    """True se o nome parece um nome humano (só letras, espaços, acentos, hifens)."""
    if not nome or len(nome.strip()) < 3:
        return False
    permitidas = {"Ll", "Lu", "Lt", "Lm", "Lo", "Zs", "Pd"}  # letras + espaço + hífen
    for ch in nome.strip():
        if unicodedata.category(ch) not in permitidas:
            return False
    return True


# ─── external APIs ──────────────────────────────────────────────────────────

def lookup_cpf(cpf: str) -> dict:
    """Retorna {nome_completo, data_nascimento, genero} via CPF Hub."""
    cpf_digits = re.sub(r"\D", "", cpf)
    if len(cpf_digits) != 11:
        raise ValueError(f"CPF inválido: {cpf}")
    resp = httpx.get(
        f"https://api.cpfhub.io/cpf/{cpf_digits}",
        headers={"x-api-key": CPFHUB_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"CPF Hub erro: {data}")
    d = data["data"]
    return {
        "nome_completo": d.get("name", ""),
        "data_nascimento": d.get("birthDate", ""),
        "genero": d.get("gender", ""),
    }


def lookup_cep(cep: str) -> dict:
    """Retorna {endereco, cidade, estado, cep} via ViaCEP."""
    cep_digits = re.sub(r"\D", "", cep)
    if len(cep_digits) != 8:
        raise ValueError(f"CEP inválido: {cep}")
    resp = httpx.get(f"https://viacep.com.br/ws/{cep_digits}/json/", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("erro"):
        raise RuntimeError(f"CEP não encontrado: {cep}")
    rua    = data.get("logradouro", "")
    bairro = data.get("bairro", "")
    endereco = f"{rua}, {bairro}" if rua else bairro
    return {
        "endereco": endereco,
        "cidade":   data.get("localidade", ""),
        "estado":   data.get("uf", ""),
        "cep":      cep_digits,
    }


# ─── HTML + DocuSeal ────────────────────────────────────────────────────────

def _gerar_html_procuracao(tipo: str, lead: dict) -> str:
    objeto = OBJETOS.get(tipo, "")
    if not objeto:
        raise ValueError(f"Tipo inválido: {tipo}. Opções: {list(OBJETOS)}")

    nome        = lead.get("nome_completo", "")
    cpf         = lead.get("cpf", "")
    rg          = lead.get("rg", "")
    orgao_rg    = lead.get("orgao_rg", "SSP-CE")
    nascimento  = lead.get("data_nascimento", "")
    estado_civil= lead.get("estado_civil", "")
    endereco    = lead.get("endereco", "")
    cidade      = lead.get("cidade", "")
    estado      = lead.get("estado", "")
    genero      = lead.get("genero", "M")
    pronome     = "o" if genero == "M" else "a"
    nat         = "brasileiro" if genero == "M" else "brasileira"
    data_doc    = _data_hoje_pt()

    # linha de endereço — omite se vazia
    linha_end = (
        f"residente e domiciliad{pronome}(a) na <strong>{endereco}</strong>, "
        f"<strong>{cidade}/{estado}</strong>,"
        if endereco else
        "residente e domiciliad{p}(a) em endereço a ser informado,".format(p=pronome)
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><style>
  body {{ font-family: Arial, sans-serif; font-size: 12pt; line-height: 1.8;
         margin: 40px; color: #000; }}
  h1 {{ text-align: center; font-size: 14pt; margin-bottom: 30px; }}
  p {{ text-align: justify; margin-bottom: 12px; }}
  .assinatura {{ text-align: center; margin-top: 60px; }}
</style></head>
<body>
<h1>PROCURAÇÃO AD JUDICIA ET EXTRA</h1>

<p>Por esta e pela melhor forma de direito, eu, <strong>{nome}</strong>,
{nat}(a), {estado_civil}, portador(a) do CPF n.º <strong>{cpf}</strong>
e do RG n.º <strong>{rg}</strong> expedido pelo <strong>{orgao_rg}</strong>,
nascid{pronome}(a) em <strong>{nascimento}</strong>,
{linha_end}</p>

<p><strong>NOMEIO E CONSTITUO</strong> como meu bastante Procurador:</p>

<p><strong>{DR_NOME}</strong>, inscrit{pronome}(a) na <strong>{DR_OAB}</strong>,
com escritório em <strong>{DR_CIDADE}</strong>,</p>

<p>ao qual outorgo plenos poderes para: {objeto}</p>

<p>Esta procuração é outorgada com a cláusula <em>ad judicia et extra</em>,
com poderes gerais para o foro e poderes especiais para: receber citação,
confessar, reconhecer a procedência do pedido, transigir, desistir, renunciar
ao direito sobre o qual se funda a ação, receber e dar quitação, firmar
compromisso, substabelecer com ou sem reserva de iguais poderes.</p>

<p>{DR_CIDADE}, {data_doc}.</p>

<div class="assinatura">
  <signature-field name="Assinatura" role="First Party"
    style="width:300px;height:70px"></signature-field>
  <p><strong>{nome}</strong><br>CPF: {cpf}</p>
</div>
</body>
</html>"""


def criar_link_procuracao(tipo: str, lead_data: dict) -> str:
    """Gera procuração HTML e cria submission no DocuSeal Cloud → retorna signing URL."""
    api_key = DOCUSEAL_CLOUD_API_KEY
    if not api_key:
        raise RuntimeError("DOCUSEAL_CLOUD_API_KEY não configurada")

    html = _gerar_html_procuracao(tipo, lead_data)
    cpf_digits = re.sub(r"\D", "", lead_data.get("cpf", ""))

    payload = {
        "documents": [{"name": f"procuracao_{tipo}", "html": html}],
        "submitters": [{
            "role":        "First Party",
            "name":        lead_data.get("nome_completo", ""),
            "external_id": cpf_digits,
            "send_email":  False,
        }],
    }

    resp = httpx.post(
        f"{DOCUSEAL_CLOUD_URL}/submissions/html",
        headers={"X-Auth-Token": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"DocuSeal Cloud erro {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    submitters = data.get("submitters", [])
    if submitters:
        slug = submitters[0].get("slug", "")
        if slug:
            return f"https://docuseal.com/s/{slug}"
    raise RuntimeError(f"DocuSeal não retornou signing URL: {data}")


def verificar_assinatura_por_slug(slug: str) -> str:
    """Verifica status da assinatura pelo slug do link DocuSeal.

    Retorna: 'ASSINADO' | 'PENDENTE' | 'NAO_ENCONTRADO'
    """
    api_key = DOCUSEAL_CLOUD_API_KEY
    if not api_key:
        raise RuntimeError("DOCUSEAL_CLOUD_API_KEY não configurada")

    slug = slug.strip().rstrip("/").split("/")[-1]

    resp = httpx.get(
        f"{DOCUSEAL_CLOUD_URL}/submitters",
        headers={"X-Auth-Token": api_key},
        params={"slug": slug, "limit": 1},
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"DocuSeal erro {resp.status_code}: {resp.text[:100]}")

    data = resp.json()
    submitters = data.get("data", []) if isinstance(data, dict) else data
    if not submitters:
        return "NAO_ENCONTRADO"

    return "ASSINADO" if submitters[0].get("status") == "completed" else "PENDENTE"
