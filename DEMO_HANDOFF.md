# Snip conversation demo handoff

Run the app and Ollama on the same teammate's computer. The app uses localhost and does not need a cloud AI account.

## Get the fix

From a clean checkout of CodeSense:

```powershell
git fetch origin
git switch fix/snip-conversation-integration
npm ci
npm run build
```

If the Python environment is not already set up:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

Open Ollama. Check that the installed model name matches the app configuration:

```powershell
ollama list
```

The default is `qwen2.5-coder:3b`. If that model is missing, download it before the offline demo:

```powershell
ollama pull qwen2.5-coder:3b
```

If using another installed **local** model, set `$env:OLLAMA_MODEL` to its exact name before starting the backend. Use one backend process. Stop an old server on port 8000 before starting the updated one:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

Open **http://127.0.0.1:8000**. Check **http://127.0.0.1:8000/api/health**: AI status must be `ready`. Rebuild and restart after pulling changes.

## Five-minute smoke test

Paste this Python code:

```python
def calculate_total(prices):
    total = 0
    for price in prices:
        total += price
    return total

prices = [10, 20, 30]
print(calculate_total(prices))
```

1. Choose **This file** under Analyze, then Explain. Check the overview, app role, concept, steps, glossary, source locations, limitations and AI context disclosure.
2. Open Q&A. Ask “What does this function do?”, then “Why does it start at zero?”, then “Explain that more simply.” Recent turns and a matching initial explanation should reach Snip.
3. Switch to Explanation and back. The conversation and an unsent draft should remain.
4. Select a few lines, choose **Selected lines** under Analyze, then ask about them. Changing scope starts a fresh conversation. File scope deliberately uses the whole file even when text is highlighted.
5. Choose **Whole project**, then ask about the project. The UI reports the bounded source excerpts actually shown.
6. Stop Ollama and submit a question. An error should appear while previous answers remain. Restart Ollama and recheck AI status.
7. Generate a missing-line challenge and restore the line in the same editor. Check hints and verification still work.

## What changed

- Recent question/answer pairs now reach Ollama, within the existing input budget. Older material is reduced explicitly.
- Scope and difficulty are part of conversation identity. Uploading a project or changing file, selection, scope or difficulty clears the thread; stale replies are ignored.
- Matching initial analysis prose is supplied as unverified context for follow-ups.
- Safe Markdown displays headings, lists and fenced code. Raw HTML, images and arbitrary AI links are not activated.
- AI source references are accepted only within source ranges actually supplied. Location verification is not a correctness claim.
- Large JavaScript analysis reuses tokenization within the same immutable project file.

## Validation boundary

Automated tests exercise the real Ollama adapter with simulated local HTTP responses, the actual React workspace hook, safe rendering, error recovery and budget enforcement. The production build is checked. This development computer does not have Ollama/Qwen; **run the smoke test on the teammate's model-enabled computer before presenting**. Model answer quality and latency remain dependent on that computer and model.
