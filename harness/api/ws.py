"""WebSocket endpoint for real-time event streaming.

Authentication is performed by AuthMiddleware (same JWT as HTTP). Browsers
cannot set Authorization on a handshake, so the token travels in
``Sec-WebSocket-Protocol`` as ``jagent.token.<jwt>`` and the server accepts
with ``jagent.jwt``.
"""

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from harness.api.auth.base import ws_accept_subprotocol
from harness.api.deps import HarnessAPI, get_hapi
from harness.core.tenant import current_principal, current_tenant_id

router = APIRouter()


@router.websocket("/api/v1/runs/{run_id}/events")
async def ws_events(websocket: WebSocket, run_id: str, api: HarnessAPI = Depends(get_hapi)):
    """Stream events for a run over WebSocket.

    Connection lifecycle:
      1. Accept the WebSocket upgrade (after middleware authentication)
      2. Send all existing events (seq order, no pagination)
      3. Stay open; new events arrive via broadcast_event() from tool layer
      4. On disconnect: remove from client list using list comprehension
    """
    principal = current_principal.get()
    if principal is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    events = await api.store.get_events(run_id)
    if not events:
        await websocket.close(code=4404, reason="Run not found")
        return

    await websocket.accept(subprotocol=ws_accept_subprotocol(websocket.scope))

    ws_clients = api._ws_clients.setdefault(run_id, [])
    ws_clients.append(websocket)
    api._ws_client_tenants[id(websocket)] = current_tenant_id()

    try:
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
        api._ws_client_tenants.pop(id(websocket), None)
        ws_clients[:] = [w for w in ws_clients if w is not websocket]
        if not ws_clients:
            api._ws_clients.pop(run_id, None)
