#!/usr/bin/env python3
"""Verifica se algum sistema está com dado defasado e gera relatório (usado pelo workflow).

Saída: imprime o relatório em Markdown; código de saída 0 sempre.
Escreve "stale=true" em $GITHUB_OUTPUT quando algum sistema passa do limite.
"""
import json
import os
from datetime import date
from pathlib import Path

LIMITE_DIAS = {"rmsp": 4, "rmbh": 7, "df": 21}  # ADASA publica com atraso de algumas semanas

dados = json.loads((Path(__file__).parent.parent / "data" / "reservatorios.json").read_text(encoding="utf-8"))
hoje = date.today()
linhas, defasados = [], []
for s in dados["sistemas"]:
    try:
        dias = (hoje - date.fromisoformat(s.get("updated_at") or "")).days
    except ValueError:
        dias = None
    limite = LIMITE_DIAS.get(s["id"], 7)
    ok = dias is not None and dias <= limite
    if not ok:
        defasados.append(s["id"])
    linhas.append(f"| {s['nome']} ({s['empresa']}) | {s.get('updated_at') or '—'} | "
                  f"{dias if dias is not None else '—'} | {limite} | {'✅' if ok else '⚠️'} |")

relatorio = "\n".join([
    f"### Frescor dos dados — {hoje.isoformat()}",
    "", "| Sistema | Último dado | Dias | Limite | |", "|---|---|---|---|---|", *linhas, "",
    ("Sistemas defasados: " + ", ".join(defasados)) if defasados else "Todos os sistemas em dia.",
])
print(relatorio)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
        f.write(relatorio + "\n")
if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"stale={'true' if defasados else 'false'}\n")
Path("/tmp/relatorio_frescor.md").write_text(relatorio, encoding="utf-8")
