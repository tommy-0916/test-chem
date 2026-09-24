# ChemAgent Backend

FastAPI service for the separated ChemAgent frontend. The service treats the existing research and campaign CLIs as subprocess boundaries; it does not import Agent runtimes into HTTP request workers.

## Setup

Use the Python environment that already contains the Agent dependencies, then install the API dependencies:

```bash
python -m pip install -r backend/requirements.txt
export CHEMAGENT_PYTHON=/absolute/path/to/agent/python
uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

Copy the repository template to `.env` and fill only the providers you use. LLM, scholarly, Web-search, and PDF-provider credentials are loaded by Agent subprocesses from the repository-root `.env`; they are never sent to the browser. Keep the real file local and commit only `.env.example`.

Runtime state is stored under `backend/data/`: SQLite job metadata, campaign artifacts, uploads, and streamed process logs. Override it with `CHEMAGENT_BACKEND_DATA_DIR`. CORS permits local Vite origins on ports `5173` and `4173`; set a comma-separated `CHEMAGENT_CORS_ORIGINS` to replace that list.

## API

- `GET /api/v1/health`
- `GET|POST /api/v1/campaigns`
- `GET /api/v1/campaigns/{id}`
- `POST /api/v1/campaigns/{id}/cancel`
- `POST /api/v1/campaigns/{id}/observations`
- `GET /api/v1/campaigns/{id}/events`
- `POST /api/v1/uploads`
- `GET /api/v1/assets/workstation-map`

Campaign detail responses always contain `job`, `research`, `device`, `campaign`, and `logs`. The SSE endpoint sends that same object as default `message` event data, so browser clients can use `EventSource.onmessage` directly.

Stable response blocks carry `schema_version: "1.0"`. Known research fields are normalized, while future Agent fields are exposed under `research.extensions` with nesting, size, and sensitive-key filtering. Campaign domain exit codes are checked against `campaign_summary.json`; a mismatched or malformed result is reported as a backend failure.

`research_preview` always invokes the research CLI with `--disable-llm --no-ledger`; online literature, Web search, and open-access PDF downloads follow the request options in both modes. `download_pdfs` requires `online_literature=true`, while Web search can run independently. Each job receives an isolated knowledge base under `backend/data/campaigns/<id>/knowledge_base`. `full_campaign` invokes `run_campaign.py`; only `mock` and `manual` execution adapters are accepted. A manual campaign receives results through the local API, which claims each iteration once and atomically writes its `observation_in.json`.

```json
{
  "query": "NiFe PBA synthesis",
  "mode": "research_preview",
  "online_literature": true,
  "web_search": true,
  "download_pdfs": true
}
```

Uploads are limited to PDF, TXT, Markdown, and JSON files no larger than 25 MB. Local reference paths must resolve inside this repository or the managed uploads directory. Model credentials remain server-side and are never accepted in campaign request bodies.

The subprocess registry is in-process, so run one Uvicorn worker. The API does not yet implement authentication; keep it bound to `127.0.0.1` for local testing, or place authentication and TLS at a trusted reverse proxy before exposing it to a network.

## Tests

```bash
python -m pytest backend/test_api.py
```

Tests inject short-lived fake commands. They do not start the real research/device agents or make LLM, scholarly API, or web-search requests.
