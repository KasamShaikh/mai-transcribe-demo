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
```

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
app.py            FastAPI backend: keyless token + fast-transcription proxy + static hosting
public/           Front-end (index.html, app.js, styles.css)
Dockerfile        Container image (python:3.12-slim, uvicorn on :8000)
requirements.txt  Python dependencies
.env.example      Configuration template
```

## Notes
- MAI-Transcribe-2 is in **public preview**. It auto-detects language and does **not** return a
  usable confidence score, so the UI hides that field when it's absent.
- Why not Azure Static Web Apps? The SWA `/api` route caps each request at **45 seconds**;
  transcription can take longer, so a container (ACA / App Service) is the better fit.
