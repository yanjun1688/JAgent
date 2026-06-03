"""WebSocket endpoint for real-time event streaming.

Architecture §8.3.2:
  - On connect: sends all historical events for the run (seq-ordered)
  - On new event: HarnessAPI.broadcast_event() pushes to all connected clients
  - Ping/pong keepalive; stale clients cleaned up via on_close
"""

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from harness.api.deps import HarnessAPI, get_hapi

router = APIRouter()


@router.websocket("/api/v1/runs/{run_id}/events")
async def ws_events(websocket: WebSocket, run_id: str, api: HarnessAPI = Depends(get_hapi)):
    """Stream events for a run over WebSocket.

    Connection lifecycle:
      1. Accept the WebSocket upgrade
      2. Send all existing events (seq order, no pagination)
      3. Stay open; new events arrive via broadcast_event() from tool layer
      4. On disconnect: remove from client list using list comprehension (avoids iteration mutation bugs)
    """
    await websocket.accept()

    ws_clients = api._ws_clients.setdefault(run_id, [])
    ws_clients.append(websocket)

    try:
        events = await api.store.get_events(run_id)
        for e in events:
            await websocket.send_json(e.model_dump(mode="json"))

        while True:
            try:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
            except WebSocketDisconnect:
                break
    except Exception:
        pass
    finally:
        ws_clients[:] = [w for w in ws_clients if w is not websocket]
