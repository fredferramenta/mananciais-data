#!/usr/bin/env python3
from __future__ import annotations
"""
Scraper de nível dos reservatórios: SABESP (RM SP), COPASA (RM BH), CAESB (DF).
Executa diariamente via GitHub Actions.
Saída: data/reservatorios.json
"""

import json
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "reservatorios.json"
HISTORY_DAYS = 730  # 2 anos de histórico

# Sistemas SABESP
SABESP_SISTEMA_INTEGRADO_ID = 75
SABESP_SISTEMAS = {
    64: "Cantareira",
    65: "Alto Tietê",
    66: "Guarapiranga",
    67: "Cotia",
    68: "Rio Grande",
    69: "Rio Claro",
    72: "São Lourenço",
}

SABESP_HEADERS = {
    "Referer": "https://mananciais.sabesp.com.br/",
    "Accept":  "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; SaneaBR-Scraper/1.0)",
}
SABESP_BASE = "https://mananciais.sabesp.com.br/api/v4"

# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def parse_pct(s: str) -> float | None:
    """'82,7 %' → 82.7"""
    s = s.replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except Exception:
        return None


def update_historico(existing: list[dict], new_entries: dict[str, float], today_str: str) -> list[dict]:
    """Mescla novas entradas {data: pct} ao histórico existente e limita a HISTORY_DAYS."""
    m = {item["data"]: item["volume_pct"] for item in existing}
    m.update({d: round(v, 2) for d, v in new_entries.items() if v is not None})
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    cutoff   = today_dt - timedelta(days=HISTORY_DAYS)
    return [
        {"data": d, "volume_pct": v}
        for d, v in sorted(m.items())
        if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff
    ]


def parse_copasa_date(header: str, reference_year: int) -> str | None:
    """'03/Junho' → '2026-06-03'"""
    months = {"janeiro":1,"fevereiro":2,"março":3,"abril":4,"maio":5,"junho":6,
              "julho":7,"agosto":8,"setembro":9,"outubro":10,"novembro":11,"dezembro":12}
    m = re.match(r"(\d{1,2})/(\w+)", header.strip(), re.IGNORECASE)
    if not m:
        return None
    day  = int(m.group(1))
    mon  = months.get(m.group(2).lower())
    if not mon:
        return None
    return f"{reference_year}-{mon:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# SABESP (RM São Paulo) — REST API oficial
# ---------------------------------------------------------------------------

def fetch_sabesp_latest_date() -> str:
    resp = requests.get(f"{SABESP_BASE}/dados/ultima-data", headers=SABESP_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]


def fetch_sabesp_day(day: str) -> dict:
    resp = requests.get(
        f"{SABESP_BASE}/sistemas/dados/resumo-diario/{day}",
        headers=SABESP_HEADERS, timeout=15,
    )
    if resp.status_code != 200:
        return {}
    return {
        item["idSistema"]: round(item["volumeUtilArmazenadoPorcentagem"], 2)
        for item in resp.json().get("data", [])
        if item.get("volumeUtilArmazenadoPorcentagem") is not None
    }


def build_sabesp_history(existing_historico: list[dict], today_str: str) -> list[dict]:
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    existing_map = {item["data"]: item["volume_pct"] for item in existing_historico}

    last_date = (
        max(datetime.strptime(d, "%Y-%m-%d").date() for d in existing_map)
        if existing_map else today_dt - timedelta(days=365)
    )

    current  = last_date + timedelta(days=1)
    fetched  = 0
    MAX_DAYS = 120  # busca no máximo 120 dias novos por execução

    while current <= today_dt and fetched < MAX_DAYS:
        day_str = current.strftime("%Y-%m-%d")
        if day_str not in existing_map:
            try:
                result = fetch_sabesp_day(day_str)
                pct = result.get(SABESP_SISTEMA_INTEGRADO_ID)
                if pct is not None:
                    existing_map[day_str] = pct
                    fetched += 1
            except Exception as e:
                print(f"  skip {day_str}: {e}", file=sys.stderr)
        current += timedelta(days=1)

    cutoff = today_dt - timedelta(days=HISTORY_DAYS)
    return [
        {"data": d, "volume_pct": v}
        for d, v in sorted(existing_map.items())
        if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff
    ]


def fetch_sabesp_subsystems(today_str: str) -> list[dict]:
    result = fetch_sabesp_day(today_str)
    out = []
    for sid, nome in SABESP_SISTEMAS.items():
        pct = result.get(sid)
        if pct is not None:
            slug = nome.lower().replace(" ", "_").replace("ê","e").replace("ã","a")
            out.append({"id": slug, "nome": nome, "volume_pct": pct})
    return out


# ---------------------------------------------------------------------------
# COPASA (RM Belo Horizonte) — HTML estático com BeautifulSoup
# ---------------------------------------------------------------------------

def fetch_copasa(today_str: str) -> dict | None:
    """
    A página da COPASA publica tabela HTML estática com 3 dias de dados.
    Usa BeautifulSoup para parsear a tabela de reservatórios diretamente.
    Retorna: {"volume_pct": float, "reservatorios": [...], "historico_multi": {data: pct}}
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        try:
            import subprocess, sys as _sys
            subprocess.check_call([_sys.executable, "-m", "pip", "install", "beautifulsoup4", "-q"])
            from bs4 import BeautifulSoup
        except Exception:
            print("  beautifulsoup4 não disponível", file=sys.stderr)
            return None

    url = "https://www.copasa.com.br/wps/portal/internet/abastecimento-de-agua/nivel-dos-reservatorios"
    today_year = int(today_str[:4])

    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  COPASA HTTP erro: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    # A tabela de reservatórios tem exatamente 5 linhas: header + 4 dados
    target_table = None
    for t in tables:
        rows = t.find_all("tr")
        if len(rows) == 5:
            # Verifica se parece com a tabela correta (tem % nas células)
            cell_texts = [td.get_text(strip=True) for td in rows[1].find_all(["td","th"])]
            if any("%" in c for c in cell_texts):
                target_table = t
                break

    if target_table is None:
        print("  COPASA: tabela de reservatórios não encontrada.", file=sys.stderr)
        return None

    all_rows = target_table.find_all("tr")
    # Linha 0: header com datas
    header_cells = [td.get_text(strip=True).replace("\xa0","") for td in all_rows[0].find_all(["td","th"])]
    dates_parsed = []
    for cell in header_cells[1:]:  # pula primeira célula vazia
        ds = parse_copasa_date(cell, today_year)
        dates_parsed.append(ds)

    # Mapeamento de nomes da página → slug + nome display
    NOME_MAP = {
        "sistema paraopeba": ("sistema_paraopeba", "Sistema Paraopeba"),
        "rio manso":         ("rio_manso",          "Rio Manso"),
        "serra azul":        ("serra_azul",          "Serra Azul"),
        "vargem das flores": ("vargem_das_flores",   "Vargem das Flores"),
    }

    parsed_rows: dict[str, list[float | None]] = {}
    for tr in all_rows[1:]:
        cells = [td.get_text(strip=True).replace("\xa0", " ") for td in tr.find_all(["td","th"])]
        if not cells:
            continue
        nome_key = cells[0].strip().lower()
        vals = [parse_pct(c) for c in cells[1:]]
        if nome_key in NOME_MAP:
            parsed_rows[nome_key] = vals

    print(f"  COPASA datas: {dates_parsed}")
    for k, v in parsed_rows.items():
        print(f"    {k}: {v}")

    main_key  = "sistema paraopeba"
    main_vals = parsed_rows.get(main_key, [])
    main_pct  = next((v for v in reversed(main_vals) if v is not None), None)

    if main_pct is None or not (5.0 <= main_pct <= 105.0):
        print(f"  COPASA: valor principal inválido ({main_pct})", file=sys.stderr)
        return None

    # Reservatórios individuais
    reservatorios = []
    for nome_key, (slug, nome_display) in NOME_MAP.items():
        if nome_key == main_key:
            continue
        vals = parsed_rows.get(nome_key, [])
        pct  = next((v for v in reversed(vals) if v is not None), None)
        if pct is not None:
            reservatorios.append({"id": slug, "nome": nome_display, "volume_pct": pct})

    # Histórico multi-data (últimos 3 dias da tabela para o sistema principal)
    historico_multi: dict[str, float] = {}
    for i, ds in enumerate(dates_parsed):
        if ds and i < len(main_vals) and main_vals[i] is not None:
            historico_multi[ds] = main_vals[i]

    return {
        "volume_pct":      main_pct,
        "reservatorios":   reservatorios,
        "historico_multi": historico_multi,
    }


# ---------------------------------------------------------------------------
# CAESB (Distrito Federal) — Playwright
# ---------------------------------------------------------------------------

def fetch_caesb() -> dict | None:
    """
    Reservatórios do sistema de abastecimento do DF (CAESB):
      Descoberto, Santa Maria, Corumbá IV, Torto/Bananal, Parananoá
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright não instalado — CAESB ignorada.", file=sys.stderr)
        return None

    url = "https://www.caesb.df.gov.br/barragens-da-caesb/"
    RESERV_NAMES = [
        ("descoberto",    "Descoberto"),
        ("santa_maria",   "Santa Maria"),
        ("corumba_iv",    "Corumbá IV"),
        ("torto_bananal", "Torto/Bananal"),
        ("paranoa",       "Parananoá"),
    ]

    try:
        import time

        ajax_responses: list[str] = []

        def capture_ajax(response: object) -> None:
            if "admin-ajax" in response.url and response.status == 200:
                try:
                    ajax_responses.append(response.text())
                except Exception:
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("response", capture_ajax)
            page.goto(url, wait_until="networkidle", timeout=45000)
            # Scroll para disparar lazy-loading de todos os gráficos
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(4)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(4)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(10)  # aguarda todas as requisições AJAX completarem

            # 1. Tenta ApexCharts global
            chart_series = page.evaluate("""
                () => {
                    const out = [];
                    if (window.Apex && window.Apex._chartInstances) {
                        for (const inst of Object.values(window.Apex._chartInstances)) {
                            try { out.push({series: inst.w.config.series, labels: inst.w.config.labels || []}); }
                            catch(e) {}
                        }
                    }
                    return out;
                }
            """)

            # 2. Tenta textos SVG (valores exibidos nos gauges/barras)
            svg_texts = page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    'svg text, .apexcharts-datalabel, .apexcharts-text, [class*="apexcharts"] text'
                )).map(e => e.textContent.trim()).filter(t => t.length > 0)
            """)

            # 3. Intercepta dados via admin-ajax (Graphina carrega via AJAX)
            ajax_data = page.evaluate("""
                () => {
                    const el = document.querySelector('[data-graphina-chart-data]');
                    return el ? el.getAttribute('data-graphina-chart-data') : null;
                }
            """)

            all_html = page.content()
            browser.close()

            reservatorios, main_pct = [], None

            def is_css_frac(v: float) -> bool:
                for d in range(2, 13):
                    for n in range(1, d):
                        if abs(v - round(100 * n / d, 2)) < 0.06:
                            return True
                return False

            def clean_pcts(values: list, label: str) -> list[float]:
                seen, out = set(), []
                for v in values:
                    try:
                        f = round(float(str(v).replace(",", ".")), 2)
                        if 5.0 <= f <= 105.0 and not is_css_frac(f) and f not in seen:
                            seen.add(f)
                            out.append(f)
                    except Exception:
                        pass
                if out:
                    print(f"  {label}: {out[:8]}")
                return out

            # 0. Tenta dados AJAX interceptados (Graphina carrega via admin-ajax.php)
            if not reservatorios and ajax_responses:
                print(f"  AJAX respostas capturadas: {len(ajax_responses)}")
                for resp_text in ajax_responses:
                    try:
                        aj = json.loads(resp_text)
                        # Graphina retorna {"success": true, "data": {"series": [...]}}
                        series_data = None
                        if isinstance(aj, dict):
                            series_data = (aj.get("data") or {}).get("series") or aj.get("series")
                        if series_data:
                            raw_vals = []
                            for s in (series_data if isinstance(series_data, list) else []):
                                if isinstance(s, dict):
                                    for v in (s.get("data") or []):
                                        if isinstance(v, (int, float)):
                                            raw_vals.append(v)
                                elif isinstance(s, (int, float)):
                                    raw_vals.append(s)
                            ajax_clean = clean_pcts(raw_vals, "AJAX data")
                            if ajax_clean:
                                for i, (slug, nome) in enumerate(RESERV_NAMES):
                                    if i < len(ajax_clean):
                                        reservatorios.append({"id": slug, "nome": nome, "volume_pct": ajax_clean[i]})
                                break
                    except Exception:
                        pass

            # Tenta ApexCharts primeiro
            if chart_series:
                print(f"  ApexCharts: {len(chart_series)} instâncias")
                raw_vals = []
                for c in chart_series:
                    for s in (c.get("series") or []):
                        data = s.get("data", []) if isinstance(s, dict) else [s]
                        for v in (data if isinstance(data, list) else []):
                            if isinstance(v, (int, float)):
                                raw_vals.append(v)
                all_vals = clean_pcts(raw_vals, "ApexCharts vals")
                if all_vals:
                    for i, (slug, nome) in enumerate(RESERV_NAMES):
                        if i < len(all_vals):
                            reservatorios.append({"id": slug, "nome": nome, "volume_pct": all_vals[i]})

            # Tenta SVG texts
            if not reservatorios and svg_texts:
                all_vals = clean_pcts(svg_texts, "SVG texts")
                if all_vals:
                    for i, (slug, nome) in enumerate(RESERV_NAMES):
                        if i < len(all_vals):
                            reservatorios.append({"id": slug, "nome": nome, "volume_pct": all_vals[i]})

            # Fallback HTML: busca padrões "NomeReserv ... XX%" mas APENAS
            # em contexto de nível/volume, não de capacidade/abastecimento
            if not reservatorios:
                # Remove trechos de texto que descrevem capacidade do sistema (não nível)
                html_clean = re.sub(
                    r'responsável por[^<]{0,200}%|abastecimento[^<]{0,200}%'
                    r'|capacidade[^<]{0,200}%|produção[^<]{0,200}%',
                    '', all_html, flags=re.IGNORECASE
                )
                for slug, nome in RESERV_NAMES:
                    patterns = [
                        rf"{re.escape(nome)}[^<]{{0,300}}?(\d+(?:[,\.]\d+)?)\s*%",
                        rf"(\d+(?:[,\.]\d+)?)\s*%[^<]{{0,200}}?{re.escape(nome)}",
                    ]
                    for pat in patterns:
                        m_html = re.search(pat, html_clean, re.IGNORECASE | re.DOTALL)
                        if m_html:
                            v = parse_pct(m_html.group(1))
                            if v and 15.0 <= v <= 105.0 and not is_css_frac(v):
                                reservatorios.append({"id": slug, "nome": nome, "volume_pct": v})
                                break

            if len(reservatorios) < 3:
                print(f"  CAESB: apenas {len(reservatorios)} reservatório(s) encontrado(s) — dados insuficientes.", file=sys.stderr)
                return None

            vals = [r["volume_pct"] for r in reservatorios]
            main_pct = round(sum(vals) / len(vals), 2)

            if not (5.0 <= main_pct <= 105.0):
                print(f"  CAESB dados suspeitos (avg={main_pct})", file=sys.stderr)
                return None

            print(f"  CAESB: {main_pct:.1f}% — {len(reservatorios)} reservatórios")
            return {"volume_pct": main_pct, "reservatorios": reservatorios}

    except Exception as e:
        print(f"Erro ao buscar CAESB: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def load_existing() -> dict:
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": "", "version": 1, "sistemas": []}


def find_sistema(existing: dict, sid: str) -> dict:
    for s in existing.get("sistemas", []):
        if s["id"] == sid:
            return s
    return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Scraper Mananciais ===")
    today_str = date.today().strftime("%Y-%m-%d")
    existing  = load_existing()
    output_sistemas = []

    # ── SABESP / RM SP ──────────────────────────────────────────────────────
    print("\n→ SABESP (RM São Paulo)…")
    try:
        latest = fetch_sabesp_latest_date()
        print(f"  Última data API: {latest}")
        ex = find_sistema(existing, "rmsp")
        hist = build_sabesp_history(ex.get("historico", []), latest)
        day_data   = fetch_sabesp_day(latest)
        volume_pct = day_data.get(SABESP_SISTEMA_INTEGRADO_ID, 0.0)
        reserv     = fetch_sabesp_subsystems(latest)
        print(f"  Sistema Integrado: {volume_pct:.1f}% | {len(hist)} dias histórico")
        output_sistemas.append({
            "id":           "rmsp",
            "abbreviation": "SP",
            "nome":         "RM São Paulo",
            "empresa":      "SABESP",
            "volume_pct":   round(volume_pct, 2),
            "updated_at":   latest,
            "reservatorios": reserv,
            "historico":    hist,
        })
    except Exception as e:
        print(f"  ERRO: {e}", file=sys.stderr)
        ex = find_sistema(existing, "rmsp")
        if ex:
            output_sistemas.append(ex)

    # ── COPASA / RM BH ──────────────────────────────────────────────────────
    print("\n→ COPASA (RM Belo Horizonte)…")
    ex = find_sistema(existing, "rmbh")
    copasa = fetch_copasa(today_str)
    if copasa:
        hist = update_historico(ex.get("historico", []), copasa["historico_multi"], today_str)
        print(f"  Sistema Paraopeba: {copasa['volume_pct']:.1f}% | {len(hist)} dias histórico")
        output_sistemas.append({
            "id":           "rmbh",
            "abbreviation": "BH",
            "nome":         "RM Belo Horizonte",
            "empresa":      "COPASA",
            "volume_pct":   copasa["volume_pct"],
            "updated_at":   today_str,
            "reservatorios": copasa["reservatorios"],
            "historico":    hist,
        })
    else:
        print("  Mantendo dados anteriores.")
        if ex:
            output_sistemas.append(ex)
        else:
            output_sistemas.append({
                "id": "rmbh", "abbreviation": "BH", "nome": "RM Belo Horizonte",
                "empresa": "COPASA", "volume_pct": None, "updated_at": "",
                "reservatorios": [], "historico": [],
            })

    # ── CAESB / DF ──────────────────────────────────────────────────────────
    print("\n→ CAESB (Distrito Federal)…")
    ex = find_sistema(existing, "df")
    caesb = fetch_caesb()
    if caesb:
        hist = update_historico(ex.get("historico", []), {today_str: caesb["volume_pct"]}, today_str)
        output_sistemas.append({
            "id":           "df",
            "abbreviation": "DF",
            "nome":         "Distrito Federal",
            "empresa":      "CAESB",
            "volume_pct":   caesb["volume_pct"],
            "updated_at":   today_str,
            "reservatorios": caesb["reservatorios"],
            "historico":    hist,
        })
    else:
        print("  Mantendo dados anteriores.")
        if ex:
            output_sistemas.append(ex)
        else:
            output_sistemas.append({
                "id": "df", "abbreviation": "DF", "nome": "Distrito Federal",
                "empresa": "CAESB", "volume_pct": None, "updated_at": "",
                "reservatorios": [], "historico": [],
            })

    # ── Salva ────────────────────────────────────────────────────────────────
    output = {"updated_at": today_str, "version": 1, "sistemas": output_sistemas}
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_hist = sum(len(s.get("historico", [])) for s in output_sistemas)
    print(f"\n✓ Salvo — {len(output_sistemas)} sistemas · {total_hist} pontos históricos")


if __name__ == "__main__":
    main()
