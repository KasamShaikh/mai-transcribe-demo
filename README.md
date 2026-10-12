# MAI-Transcribe-2 · Live Demo

A lightweight **FastAPI** web app that showcases **MAI-Transcribe-2** — Microsoft AI's multilingual
speech-to-text model — through the Azure Speech **fast-transcription** REST API. No Azure AI Foundry
project required: it runs against a plain **Azure Speech** (Cognitive Services) resource, fully
**keyless** with Microsoft Entra ID.

## Features
- 🎙️ Record from the mic or upload `WAV / MP3 / M4A / WebM / OGG / FLAC`
- 🌐 Automatic multilingual detection (e.g. English + Hindi + Marathi) with a per-line language tag
- 🗣️ Speaker diarization — `Speaker 1 :- …` conversation view
- 🎯 Keyword biasing for domain terms (NEFT, KYC, …)
- ⚡ Latency / real-time-factor meter
- 🔑 Keyless auth via `DefaultAzureCredential` (managed identity in the cloud, `az login` locally)

## Tabs
The app has three pages (top navigation):

- **Batch** (`index.html`) — the original one-shot flow: record or upload a file, then transcribe
  with MAI-Transcribe-2 (diarization, keyword biasing, click-to-seek playback). **Unchanged.**
- **Real time** (`realtime.html`) — live microphone transcription with three selectable pipelines.
- **Way it works** (`how-it-works.html`) — a static explainer with flow diagrams, short code snippets,
  and a comparison table for the three pipelines.

## Real time pipelines
The browser resamples the mic to **16 kHz mono PCM16** in an `AudioWorklet` and streams it over a
WebSocket (`/ws/realtime`). Azure credentials stay on the server — **no token reaches the browser**.

| Mode | How | Latency | Interim words | Speaker labels |
|------|-----|---------|---------------|----------------|
| **MAI Near-real-time** | Rolling ~8 s WAV windows → MAI-Transcribe-2 fast transcription | ~8 s | No | Off |
| **Standard Realtime** | Azure Speech SDK continuous recognition (push stream) | Instant interim | Yes | — |
| **Hybrid** | Standard interim live, then one full MAI pass replaces it on stop | Instant + final | Yes | Yes (final) |

Standard/Hybrid use the Azure Speech SDK (`azure-cognitiveservices-speech`) with keyless
`SpeechConfig(endpoint=…, token_credential=…)`. The browser needs a secure context — `localhost` or
HTTPS — for microphone access, and Azure Container Apps ingress supports the WebSocket upgrade.

## How MAI-Transcribe-2 is selected
The backend builds the fast-transcription `definition` and posts it to
`{endpoint}/speechtotext/transcriptions:transcribe`:

```json
{
  "enhancedMode": { "enabled": true, "model": "MAI-Transcribe-2" },
  "diarization":  { "enabled": true }
}
```

## Prerequisites
- An Azure **Speech** resource (kind `SpeechServices`) in a MAI-Transcribe region:
  `centralindia`, `eastus`, `northeurope`, `southeastasia`, `westus`, `westus2`.
- Your identity has the **Cognitive Services User** role on that resource.
- Python 3.11+ and the Azure CLI (run `az login`).

## Run locally
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (use: source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env            # then edit SPEECH_ENDPOINT
uvicorn app:app --host 127.0.0.1 --port 8000
```
Open <http://127.0.0.1:8000>.

### Configuration (`.env`)
```
SPEECH_ENDPOINT=https://<your-speech-resource>.cognitiveservices.azure.com
SPEECH_API_VERSION=2025-10-15
# Optional: English transcript + summary at the end of a call (Azure OpenAI, keyless;
# identity needs "Cognitive Services OpenAI User" on that resource)
AOAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com
AOAI_DEPLOYMENT=gpt-4-1-mini
```

When `AOAI_*` is set, every call (Real time in all modes, and Batch) ends with a full English
transcript (Hindi/Marathi translated) and a short summary. If unset, the feature is skipped.

### Login
The whole app (pages, APIs and the live WebSocket) sits behind a sign-in page. Credentials are
**never stored in the repo**; set them as environment variables (use a secret in Azure):
```
APP_USERNAME=<username>
APP_PASSWORD=<password>
SESSION_SECRET=<optional random string for signing session cookies>
```
If `APP_PASSWORD` is unset, the login gate is disabled (convenient for local development).

## Deploy to Azure Container Apps
Build the image remotely and deploy with a **user-assigned managed identity** (replace every
`<placeholder>`):

```bash
# Build & push
az acr build --registry <acr> --image mai-transcribe:v1 .

# Grant the identity access BEFORE creating the app
az role assignment create --assignee-object-id <uami-principal-id> --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope <acr-resource-id>
az role assignment create --assignee-object-id <uami-principal-id> --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services User" --scope <speech-resource-id>

# Create the Container App
az containerapp create -n <app> -g <rg> --environment <env> \
  --image <acr>.azurecr.io/mai-transcribe:v1 \
  --user-assigned <uami-resource-id> --registry-identity <uami-resource-id> \
  --registry-server <acr>.azurecr.io \
  --target-port 8000 --ingress external \
  --env-vars SPEECH_ENDPOINT=https://<your-speech-resource>.cognitiveservices.azure.com \
             SPEECH_API_VERSION=2025-10-15 \
             AZURE_CLIENT_ID=<uami-client-id>
```

`AZURE_CLIENT_ID` tells `DefaultAzureCredential` which user-assigned identity to use.

## Project layout
```
app.py             FastAPI backend: Batch fast-transcription proxy + static hosting (unchanged)
mai_client.py      Isolated MAI fast-transcription client used by the Real time tab
realtime.py        /ws/realtime WebSocket + the three live pipelines
public/            Front-end
  index.html, app.js, styles.css                  Batch tab (behavior unchanged)
  realtime.html, realtime.js, realtime.css        Real time tab
  pcm-capture-worklet.js                          16 kHz mic capture for the Real time tab
  how-it-works.html, how-it-works.css             Way it works tab
tests/             Batch regression + realtime unit/WebSocket tests
Dockerfile         Container image (adds Speech SDK runtime libs; copies the new modules)
requirements.txt   Python dependencies
.env.example       Configuration template
```

## Notes
- MAI-Transcribe-2 is in **public preview**. It auto-detects language and does **not** return a
  usable confidence score, so the UI hides that field when it's absent.
- Why not Azure Static Web Apps? The SWA `/api` route caps each request at **45 seconds**;
  transcription can take longer, so a container (ACA / App Service) is the better fit.
- The **Real time** tab reuses the same Speech resource and keyless identity. MAI live is
  *near*-real-time (its fast-transcription API is synchronous, so text refreshes every ~8 s);
  **Standard** is the only true interim stream. MAI diarization needs the full recording, so it runs
  in **Batch** and in **Hybrid**'s final pass — not in the rolling MAI windows.
