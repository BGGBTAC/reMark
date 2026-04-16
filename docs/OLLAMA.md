# Offline Setup with Ollama

reMark 0.8 can run without any cloud calls by routing every LLM
call — note structuring, tagging, summarising, action extraction,
reports, OCR VLM, embeddings — through a local
[Ollama](https://ollama.com) server.

## 1. Install Ollama

macOS: `brew install ollama`

Linux: `curl -fsSL https://ollama.com/install.sh | sh`

## 2. Pull the models

```bash
ollama pull llama3.1           # note structuring, tagging, summaries, actions, reports
ollama pull llava              # handwriting OCR (vision)
ollama pull nomic-embed-text   # semantic search embeddings
```

Different models work too — override them via `llm.ollama.*_model`
in `config.yaml` or from `/settings/llm` in the web dashboard.

## 3. Point reMark at Ollama

`config.yaml`:

```yaml
llm:
  provider: ollama
  ollama:
    base_url: http://localhost:11434
```

## 4. Verify

```bash
remark-bridge sync --once
remark-bridge ask "what is in my last note"
```

Both should complete without any outbound request to `api.anthropic.com`.

## Hardware notes

- A 7B-class text model (llama3.1:8b, mistral) needs ~8 GB RAM.
- Vision models (llava) need ~12 GB RAM and benefit heavily from a GPU.
- Embedding models (`nomic-embed-text`) are tiny — CPU is fine.

## Switching back

Flip `llm.provider` back to `anthropic` (or remove the block) and
reMark uses the cloud again. No data migration needed — the vault
is identical regardless of provider.
