# FounderOS — Startup CEO Agent

A focused CEO workspace powered by MemoryOS. It provides a daily briefing, decision support, company signals, and a conversational layer over persistent company memory.

## Run

From this folder, run:

```powershell
python -m http.server 4173
```

Then visit `http://localhost:4173`.

## Connect to MemoryOS

Open **Memory connection** in the app and enter:

- API URL: `http://127.0.0.1:8088`
- Workspace ID: the workspace used to create your MemoryOS API key
- User ID: the MemoryOS user whose company context you are demoing
- API key: your workspace key

The app calls `POST /v1/memories` to save new company context and `POST /v1/memories/retrieve` to answer CEO questions from MemoryOS. It retrieves only context belonging to the selected User ID. Configure a running MemoryOS instance before using the live CEO-agent features.

## What it remembers

Customers, roadmap decisions, investor meetings, company metrics, Slack conversations, and GitHub issues — all organized as durable MemoryOS context rather than a disposable chat history.
