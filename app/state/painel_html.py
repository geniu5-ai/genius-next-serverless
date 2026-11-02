"""Painel de conversao em HTML unico, para mostrar a um cliente (23/out/2026).

So numeros agregados (nunca nome, telefone ou texto de paciente). Autocontido: sem JS e sem
recurso externo. Escapa todo texto que vem de fora.
"""

from __future__ import annotations

from html import escape
from typing import Any


def _fmt_min(v: float | None) -> str:
    if v is None:
        return "sem dados"
    if v < 1:
        return f"{round(v * 60)} segundos"
    if v < 120:
        return f"{v:g} min"
    if v < 2880:
        return f"{round(v / 60)} horas"
    return f"{round(v / 1440)} dias"


def _card(rotulo: str, valor: str, nota: str = "") -> str:
    nota_html = f'<div class="nota">{escape(nota)}</div>' if nota else ""
    return f'<div class="card"><div class="valor">{escape(valor)}</div><div class="rotulo">{escape(rotulo)}</div>{nota_html}</div>'


def renderizar(p: dict[str, Any], *, cliente: str, dividas: int = 0, baseline: str = "") -> str:
    taxa = "sem dados" if p["taxa_resposta_pct"] is None else f'{p["taxa_resposta_pct"]}%'
    cards = [
        _card("dos leads foram respondidos", taxa, f'{p["respondidos"]} de {p["leads"]} nos últimos {p["dias"]} dias'),
        _card("para a primeira resposta (mediana)", _fmt_min(p["mediana_primeira_resposta_min"]), f'9 de cada 10 em até {_fmt_min(p["p90_primeira_resposta_min"])}'),
        _card("esperando resposta agora", str(dividas), "conversas em que o paciente falou por último"),
    ]
    medido = not p["nao_mede_ainda"]
    funil = (
        "".join(
            [
                _card("agendamentos registrados", str(p["agendados"])),
                _card("pagamentos confirmados", str(p["pagos"]), f'R$ {p["pago_reais"]:,.2f}'.replace(",", "X").replace(".", ",").replace("X", ".")),
                _card("compareceram", str(p["compareceram"])),
            ]
        )
        if medido
        else '<p class="aviso">Agendamentos, pagamentos e comparecimentos ainda não estão sendo medidos neste cliente. '
        "Entram quando a agenda e o Pix do Genius registrarem a prova.</p>"
    )
    base = f'<p class="base">Referência: {escape(baseline)}</p>' if baseline else ""
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atendimento {escape(cliente)}: painel</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#111}}
main{{max-width:960px;margin:0 auto;padding:32px 20px}}
h1{{font-size:26px;margin:0 0 4px}} .sub{{color:#555;margin:0 0 24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}}
.card{{background:#fff;border-radius:14px;padding:20px;box-shadow:0 1px 3px #0001}}
.valor{{font-size:38px;font-weight:700;line-height:1.1}} .rotulo{{margin-top:6px;color:#333}}
.nota{{margin-top:6px;color:#777;font-size:13px}}
h2{{font-size:18px;margin:28px 0 10px}} .aviso,.base{{color:#555;background:#fff;border-radius:12px;padding:14px 18px}}
footer{{margin-top:28px;color:#888;font-size:12px}}
</style></head><body><main>
<h1>Atendimento no WhatsApp: {escape(cliente)}</h1>
<p class="sub">Últimos {p["dias"]} dias. Números reais do atendimento; nenhum dado de paciente é exibido.</p>
<h2>Velocidade e cobertura</h2><div class="grid">{"".join(cards)}</div>{base}
<h2>Do contato ao atendimento</h2><div class="grid">{funil}</div>
<footer>Gerado pelo Genius Next. Os números vêm do banco de produção no momento da geração.</footer>
</main></body></html>"""
