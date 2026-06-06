# mananciais-data

Dados automatizados do nível dos reservatórios para o app **SaneaBR**.

## Como configurar (5 minutos)

### 1. Crie o repositório no GitHub

1. Acesse [github.com/new](https://github.com/new)
2. Nome: `mananciais-data`
3. Visibilidade: **Público** (necessário para o app acessar sem autenticação)
4. Clique em **Create repository**

### 2. Faça push deste diretório

```bash
cd "/Users/fredericodelfino/Documents/BI Saneamento/mananciais-data"
git init
git add .
git commit -m "Setup inicial do scraper de mananciais"
git branch -M main
git remote add origin https://github.com/SEU-USUARIO/mananciais-data.git
git push -u origin main
```

### 3. Atualize a URL no app iOS

Em `ReservatorioService.swift`, substitua:
```swift
static let dataURL = "https://raw.githubusercontent.com/SEU-USUARIO/mananciais-data/main/data/reservatorios.json"
```
pela URL com seu usuário GitHub real.

### 4. Habilite o GitHub Actions

- Vá em **Settings → Actions → General** no seu repositório
- Certifique-se de que **"Allow all actions"** está marcado
- Em **Settings → Actions → General → Workflow permissions**:
  selecione **"Read and write permissions"**

### 5. Execute o scraper pela primeira vez

- Vá em **Actions → Atualizar Nível dos Reservatórios → Run workflow**
- O workflow roda automaticamente toda **segunda-feira às 8h UTC**

---

## Estrutura

```
mananciais-data/
├── .github/
│   └── workflows/
│       └── scrape.yml         ← Workflow do GitHub Actions
├── scraper/
│   └── scrape.py              ← Script Python de coleta
└── data/
    └── reservatorios.json     ← Dados atualizados (atualizado pelo workflow)
```

## Fontes de dados

| Sistema | Empresa | Método |
|---------|---------|--------|
| RM São Paulo | SABESP | API REST (`mananciais.sabesp.com.br/api/v4`) |
| RM Belo Horizonte | COPASA | Playwright (página dinâmica) |
| Distrito Federal | CAESB | Playwright (WordPress/Elementor) |

## Formato do JSON

```json
{
  "updated_at": "2026-06-09",
  "version": 1,
  "sistemas": [
    {
      "id": "rmsp",
      "nome": "RM São Paulo",
      "empresa": "SABESP",
      "volume_pct": 51.44,
      "updated_at": "2026-06-06",
      "reservatorios": [
        { "id": "cantareira", "nome": "Cantareira", "volume_pct": 39.64 },
        ...
      ],
      "historico": [
        { "data": "2025-09-01", "volume_pct": 72.3 },
        ...
      ]
    }
  ]
}
```

## Notas

- **SABESP**: API oficial pública, retorna até 365 dias de histórico real.
- **COPASA/CAESB**: Dados coletados semanalmente via scraping; histórico acumula com o tempo.
- O app usa cache de 6 horas; ao abrir offline, exibe o último dado disponível.
