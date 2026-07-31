# Frontend Monitor WS Client

> 17 nodes

## Key Concepts

- **send()** (21 connections) — `web/app.js`
- **connectWs()** (11 connections) — `web/app.js`
- **connectMonitor()** (5 connections) — `web/app.js`
- **resync()** (5 connections) — `web/app.js`
- **logout()** (4 connections) — `web/app.js`
- **startHeartbeat()** (4 connections) — `web/app.js`
- **setRunning()** (4 connections) — `web/app.js`
- **requestCompact()** (4 connections) — `web/app.js`
- **attachmentLines()** (4 connections) — `web/app.js`
- **closeMonitor()** (3 connections) — `web/app.js`
- **closeWs()** (3 connections) — `web/app.js`
- **stopHeartbeat()** (3 connections) — `web/app.js`
- **reportVisibility()** (3 connections) — `web/app.js`
- **setConn()** (2 connections) — `web/app.js`
- **waitWsOpen()** (2 connections) — `web/app.js`
- **dangerHit()** (2 connections) — `web/app.js`
- **clearPendingImages()** (2 connections) — `web/app.js`

## Relationships

- [Frontend Chat UI](Frontend_Chat_UI.md) (20 shared connections)
- [Frontend Todo/Memo Actions](Frontend_Todo-Memo_Actions.md) (6 shared connections)
- [Frontend Message Stream Rendering](Frontend_Message_Stream_Rendering.md) (6 shared connections)
- [Frontend Agent/Artifact UI](Frontend_Agent-Artifact_UI.md) (4 shared connections)
- [Frontend Session List Management](Frontend_Session_List_Management.md) (3 shared connections)
- [Frontend Goal Loop Cards](Frontend_Goal_Loop_Cards.md) (1 shared connections)

## Source Files

- `web/app.js`

## Audit Trail

- EXTRACTED: 82 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*