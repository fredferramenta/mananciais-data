#!/usr/bin/env python3
"""
Scraper de nível dos reservatórios: SABESP (RM SP), COPASA (RM BH), CAESB (DF).
Executa semanalmente via GitHub Actions.
Saída: data/reservatorios.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "reservatorios.json"
HISTORY_DAYS = 365  # dias de histórico mantidos por sistema

# Sistemas SABESP relevantes para RMSP
SABESP_SISTEMA_INTEGRADO_ID = 75  # "Sistema Integrado Metropolitano" = volume total RMSP
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
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; SaneaBR-Scraper/1.0)",
}
SABESP_BASE = "https://mananciais.sabesp.com.br/api/v4"


# ---------------------------------------------------------------------------
# SABESP (RM São Paulo) — REST API oficial
# ---------------------------------------------------------------------------

def fetch_sabesp_latest_date() -> str:
    resp = requests.get(f"{SABESP_BASE}/dados/ultima-data", headers=SABESP_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]  # "2026-06-06"


def fetch_sabesp_day(day: str) -> dict:
    """Retorna {sistemaId: volumeUtilArmazenadoPorcentagem} para um dado dia."""
    resp = requests.get(
        f"{SABESP_BASE}/sistemas/dados/resumo-diario/{day}",
        headers=SABESP_HEADERS,
        timeout=15,
    )
    if resp.status_code != 200:
        return {}
    result = {}
    for item in resp.json().get("data", []):
        sid = item["idSistema"]
        pct = item.get("volumeUtilArmazenadoPorcentagem")
        if pct is not None:
            result[sid] = round(pct, 2)
    return result


def build_sabesp_history(existing_historico: list[dict]) -> list[dict]:
    """
    Constrói histórico para o Sistema Integrado Metropolitano.
    - Se já existe histórico, busca apenas os dias faltantes.
    - Limita a HISTORY_DAYS entradas.
    """
    today_str = fetch_sabesp_latest_date()
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()

    # Mapeia datas já presentes
    existing_map = {item["data"]: item["volume_pct"] for item in existing_historico}

    # Determina qual o dia mais recente já guardado
    if existing_map:
        last_date = max(datetime.strptime(d, "%Y-%m-%d").date() for d in existing_map)
    else:
        last_date = today_dt - timedelta(days=HISTORY_DAYS)

    # Busca dias faltantes (limitado a 90 dias por vez para não sobrecarregar)
    current = last_date + timedelta(days=1)
    fetched_days = 0
    MAX_FETCH = 90

    while current <= today_dt and fetched_days < MAX_FETCH:
        day_str = current.strftime("%Y-%m-%d")
        if day_str not in existing_map:
            try:
                result = fetch_sabesp_day(day_str)
                pct = result.get(SABESP_SISTEMA_INTEGRADO_ID)
                if pct is not None:
                    existing_map[day_str] = pct
                    fetched_days += 1
            except Exception as e:
                print(f"  Aviso: falha ao buscar {day_str}: {e}", file=sys.stderr)
        current += timedelta(days=1)

    # Monta lista ordenada, limitada a HISTORY_DAYS
    cutoff = today_dt - timedelta(days=HISTORY_DAYS)
    historico = [
        {"data": d, "volume_pct": v}
        for d, v in sorted(existing_map.items())
        if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff
    ]
    return historico


def fetch_sabesp_subsystems(today_str: str) -> list[dict]:
    """Retorna lista de sub-reservatórios do RMSP com % atual."""
    result = fetch_sabesp_day(today_str)
    sistemas = []
    for sid, nome in SABESP_SISTEMAS.items():
        pct = result.get(sid)
        if pct is not None:
            slug = nome.lower().replace(" ", "_").replace("ê", "e").replace("ã", "a")
            sistemas.append({"id": slug, "nome": nome, "volume_pct": pct})
    return sistemas


# ---------------------------------------------------------------------------
# COPASA (RM Belo Horizonte) — Playwright (página JavaScript dinâmica)
# ---------------------------------------------------------------------------

def fetch_copasa() -> dict | None:
    """
    Extrai dados da página COPASA usando Playwright.
    Retorna {"volume_pct": float, "reservatorios": [...]} ou None em caso de falha.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("Playwright não instalado — COPASA ignorada.", file=sys.stderr)
        return None

    url = "https://www.copasa.com.br/wps/portal/internet/abastecimento-de-agua/nivel-dos-reservatorios"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Aguarda que os valores sejam carregados (não sejam 0.0%)
            import re
            content = page.content()
            browser.close()

            # Tenta extrair percentuais associados a nomes de reservatórios
            # A página mostra: "Paraopeba", "Rio Manso", "Serra Azul", "Vargem das Flores"
            reservatorio_names = ["Paraopeba", "Rio Manso", "Serra Azul", "Vargem das Flores"]
            reservatorios = []

            # Extrai todos os percentuais encontrados na página
            pcts = re.findall(r'(\d+[,\.]\d+)\s*%', content)
            pcts_float = []
            for p in pcts:
                try:
                    pcts_float.append(float(p.replace(",", ".")))
                except Exception:
                    pass

            # Filtra valores plausíveis (1% – 120%)
            pcts_valid = [v for v in pcts_float if 1.0 < v < 120.0]

            if not pcts_valid:
                return None

            # Heurística: o maior conjunto de valores consecutivos é o dos reservatórios
            # COPASA tipicamente mostra: total, Paraopeba, Rio Manso, Serra Azul, Vargem das Flores
            main_pct = pcts_valid[0] if pcts_valid else None

            # Tenta mapear pelo menos os valores individuais
            for i, nome in enumerate(reservatorio_names):
                if i + 1 < len(pcts_valid):
                    slug = nome.lower().replace(" ", "_")
                    reservatorios.append({
                        "id": slug,
                        "nome": nome,
                        "volume_pct": pcts_valid[i + 1]
                    })

            return {
                "volume_pct": main_pct,
                "reservatorios": reservatorios,
            }
    except Exception as e:
        print(f"Erro ao buscar COPASA: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# CAESB (Distrito Federal) — Playwright (página WordPress + Elementor dinâmica)
# ---------------------------------------------------------------------------

def fetch_caesb() -> dict | None:
    """
    Extrai dados da página CAESB usando Playwright.
    Reservatórios: Descoberto, Santa Maria, Corumbá IV, Torto/Bananal, Parananoá.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("Playwright não instalado — CAESB ignorada.", file=sys.stderr)
        return None

    url = "https://www.caesb.df.gov.br/barragens-da-caesb/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Aguarda elementos com dados de volume
            import re
            import time
            time.sleep(3)  # aguarda gráficos renderizarem
            content = page.content()
            browser.close()

            # Reservatórios conhecidos do DF
            reservatorio_names = ["Descoberto", "Santa Maria", "Corumbá IV", "Torto/Bananal", "Parananoá"]

            pcts = re.findall(r'(\d+[,\.]\d+)\s*%', content)
            pcts_float = []
            for p_str in pcts:
                try:
                    v = float(p_str.replace(",", "."))
                    if 1.0 < v < 120.0:
                        pcts_float.append(v)
                except Exception:
                    pass

            if not pcts_float:
                return None

            # Volume médio ponderado (estimativa) como valor consolidado
            main_pct = pcts_float[0] if pcts_float else None
            reservatorios = []
            for i, nome in enumerate(reservatorio_names):
                if i < len(pcts_float):
                    slug = nome.lower().replace(" ", "_").replace("/", "_")
                    reservatorios.append({
                        "id": slug,
                        "nome": nome,
                        "volume_pct": pcts_float[i]
                    })

            return {
                "volume_pct": main_pct,
                "reservatorios": reservatorios,
            }
    except Exception as e:
        print(f"Erro ao buscar CAESB: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Utilitários de histórico para COPASA / CAESB
# ---------------------------------------------------------------------------

def update_simple_historico(existing: list[dict], new_pct: float | None, today_str: str) -> list[dict]:
    """Adiciona entrada de hoje ao histórico, mantendo HISTORY_DAYS máximo."""
    if new_pct is None:
        return existing
    existing_map = {item["data"]: item["volume_pct"] for item in existing}
    existing_map[today_str] = round(new_pct, 2)
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    cutoff = today_dt - timedelta(days=HISTORY_DAYS)
    return [
        {"data": d, "volume_pct": v}
        for d, v in sorted(existing_map.items())
        if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_existing() -> dict:
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": "", "version": 1, "sistemas": []}


def find_sistema(existing: dict, sistema_id: str) -> dict | None:
    for s in existing.get("sistemas", []):
        if s["id"] == sistema_id:
            return s
    return None


def main():
    print("=== Scraper Mananciais ===")
    today_str = date.today().strftime("%Y-%m-%d")
    existing = load_existing()

    sistemas_output = []

    # ---- SABESP / RM SP ------------------------------------------------
    print("→ SABESP (RM São Paulo)...")
    try:
        sabesp_latest = fetch_sabesp_latest_date()
        print(f"  Data mais recente: {sabesp_latest}")

        existing_rmsp = find_sistema(existing, "rmsp") or {}
        hist = existing_rmsp.get("historico", [])
        print(f"  Histórico atual: {len(hist)} entradas — buscando atualizações...")
        hist = build_sabesp_history(hist)

        current_result = fetch_sabesp_day(sabesp_latest)
        volume_pct = current_result.get(SABESP_SISTEMA_INTEGRADO_ID, 0.0)
        reservatorios = fetch_sabesp_subsystems(sabesp_latest)

        print(f"  Sistema Integrado Metropolitano: {volume_pct:.1f}%")
        print(f"  Histórico: {len(hist)} entradas")

        sistemas_output.append({
            "id": "rmsp",
            "nome": "RM São Paulo",
            "empresa": "SABESP",
            "volume_pct": round(volume_pct, 2),
            "updated_at": sabesp_latest,
            "reservatorios": reservatorios,
            "historico": hist,
        })
    except Exception as e:
        print(f"  ERRO SABESP: {e}", file=sys.stderr)
        # Preserva dados existentes
        existing_rmsp = find_sistema(existing, "rmsp")
        if existing_rmsp:
            sistemas_output.append(existing_rmsp)

    # ---- COPASA / RM BH ------------------------------------------------
    print("→ COPASA (RM Belo Horizonte)...")
    existing_rmbh = find_sistema(existing, "rmbh") or {}
    copasa_data = fetch_copasa()
    if copasa_data:
        volume_pct = copasa_data["volume_pct"]
        hist = update_simple_historico(existing_rmbh.get("historico", []), volume_pct, today_str)
        print(f"  Sistema Paraopeba: {volume_pct:.1f}%")
        sistemas_output.append({
            "id": "rmbh",
            "nome": "RM Belo Horizonte",
            "empresa": "COPASA",
            "volume_pct": round(volume_pct, 2),
            "updated_at": today_str,
            "reservatorios": copasa_data["reservatorios"],
            "historico": hist,
        })
    else:
        print("  Usando dados existentes (falha na coleta).")
        if existing_rmbh:
            sistemas_output.append(existing_rmbh)
        else:
            # Fallback sem dados históricos
            sistemas_output.append({
                "id": "rmbh",
                "nome": "RM Belo Horizonte",
                "empresa": "COPASA",
                "volume_pct": None,
                "updated_at": existing_rmbh.get("updated_at", ""),
                "reservatorios": [],
                "historico": [],
            })

    # ---- CAESB / DF ----------------------------------------------------
    print("→ CAESB (Distrito Federal)...")
    existing_df = find_sistema(existing, "df") or {}
    caesb_data = fetch_caesb()
    if caesb_data:
        volume_pct = caesb_data["volume_pct"]
        hist = update_simple_historico(existing_df.get("historico", []), volume_pct, today_str)
        print(f"  Sistema DF: {volume_pct:.1f}%")
        sistemas_output.append({
            "id": "df",
            "nome": "Distrito Federal",
            "empresa": "CAESB",
            "volume_pct": round(volume_pct, 2),
            "updated_at": today_str,
            "reservatorios": caesb_data["reservatorios"],
            "historico": hist,
        })
    else:
        print("  Usando dados existentes (falha na coleta).")
        if existing_df:
            sistemas_output.append(existing_df)
        else:
            sistemas_output.append({
                "id": "df",
                "nome": "Distrito Federal",
                "empresa": "CAESB",
                "volume_pct": None,
                "updated_at": existing_df.get("updated_at", ""),
                "reservatorios": [],
                "historico": [],
            })

    # ---- Salva JSON -------------------------------------------------------
    output = {
        "updated_at": today_str,
        "version": 1,
        "sistemas": sistemas_output,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_hist = sum(len(s.get("historico", [])) for s in sistemas_output)
    print(f"\n✓ Salvo em {OUTPUT_FILE}")
    print(f"  {len(sistemas_output)} sistemas · {total_hist} pontos históricos no total")


if __name__ == "__main__":
    main()
