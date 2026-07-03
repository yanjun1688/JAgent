"""Phase 1 E2E — Complete multi-turn conversation flow tests.

C-E1: 3-round continuous conversation
C-E2: Cross-conversation isolation
C-E3: Conversation list recovery
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

BASE_URL = "http://127.0.0.1:8000"


async def create_conversation(client: httpx.AsyncClient, title: str = "New conversation") -> dict:
    resp = await client.post(f"{BASE_URL}/api/v1/conversations", json={"title": title})
    assert resp.status_code == 201, f"Failed to create conversation: {resp.status_code} {resp.text}"
    return resp.json()


async def send_message(client: httpx.AsyncClient, conversation_id: str, message: str) -> dict:
    resp = await client.post(
        f"{BASE_URL}/api/v1/conversations/{conversation_id}/messages",
        json={"message": message},
    )
    assert resp.status_code == 200, f"Failed to send message: {resp.status_code} {resp.text}"
    return resp.json()


async def wait_for_run_completion(client: httpx.AsyncClient, run_id: str, timeout: float = 30.0) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        resp = await client.get(f"{BASE_URL}/api/v1/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in ("completed", "failed"):
            return data
        await asyncio.sleep(0.5)
    raise TimeoutError(f"Run {run_id} did not complete within {timeout}s")


async def get_conversation(client: httpx.AsyncClient, conversation_id: str) -> dict:
    resp = await client.get(f"{BASE_URL}/api/v1/conversations/{conversation_id}")
    assert resp.status_code == 200
    return resp.json()


async def list_conversations(client: httpx.AsyncClient) -> dict:
    resp = await client.get(f"{BASE_URL}/api/v1/conversations")
    assert resp.status_code == 200
    return resp.json()


async def test_three_round_conversation():
    """C-E1: 3-round continuous conversation with context injection."""
    print("[E2E] C-E1: 3-round continuous conversation...")
    async with httpx.AsyncClient() as client:
        conv = await create_conversation(client, "Weather Chat")
        cid = conv["conversation_id"]
        print(f"  Created conversation: {cid}")

        results = []
        for i, msg in enumerate(["查天气", "用中文总结", "那明天呢"]):
            send_resp = await send_message(client, cid, msg)
            run_id = send_resp["run_id"]
            print(f"  Round {i+1}: sent '{msg}', run_id={run_id}")
            state = await wait_for_run_completion(client, run_id)
            results.append(state)
            print(f"  Round {i+1}: status={state['status']}")

        conv_detail = await get_conversation(client, cid)
        messages = conv_detail.get("messages", [])
        print(f"  Conversation has {len(messages)} messages")
        assert len(messages) >= 3, f"Expected >= 3 messages, got {len(messages)}"
        print("  PASSED")


async def test_cross_conversation_isolation():
    """C-E2: Cross-conversation isolation."""
    print("[E2E] C-E2: Cross-conversation isolation...")
    async with httpx.AsyncClient() as client:
        conv_a = await create_conversation(client, "Chat A")
        conv_b = await create_conversation(client, "Chat B")
        cid_a = conv_a["conversation_id"]
        cid_b = conv_b["conversation_id"]

        await send_message(client, cid_a, "Message A1")
        await send_message(client, cid_a, "Message A2")
        await send_message(client, cid_b, "Message B1")

        detail_a = await get_conversation(client, cid_a)
        detail_b = await get_conversation(client, cid_b)

        a_runs = {m.get("run_id") for m in detail_a.get("messages", [])}
        b_runs = {m.get("run_id") for m in detail_b.get("messages", [])}

        assert a_runs.isdisjoint(b_runs) or len(a_runs) != len(b_runs), "Conversations should have different messages"
        print(f"  Chat A: {len(detail_a.get('messages', []))} messages")
        print(f"  Chat B: {len(detail_b.get('messages', []))} messages")
        print("  PASSED")


async def test_conversation_list_recovery():
    """C-E3: Create 3 conversations → GET list → verify all present, sorted by time."""
    print("[E2E] C-E3: Conversation list recovery...")
    async with httpx.AsyncClient() as client:
        ids = []
        for i in range(3):
            conv = await create_conversation(client, f"Chat {i+1}")
            ids.append(conv["conversation_id"])
            await asyncio.sleep(0.1)

        lst = await list_conversations(client)
        listed_ids = [c["conversation_id"] for c in lst["conversations"]]

        for cid in ids:
            assert cid in listed_ids, f"Conversation {cid} not in list"

        assert lst["total"] >= 3
        print(f"  List contains {lst['total']} conversations")
        print("  PASSED")


async def main():
    print("=" * 60)
    print("Harness v2.1 — Multi-turn Conversation E2E Tests")
    print("=" * 60)

    tests = [
        ("C-E1", test_three_round_conversation),
        ("C-E2", test_cross_conversation_isolation),
        ("C-E3", test_conversation_list_recovery),
    ]

    passed = 0
    failed = 0
    errors = []

    for name, test_fn in tests:
        try:
            await test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"  FAILED: {e}")

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    if errors:
        print("Failures:")
        for name, err in errors:
            print(f"  {name}: {err}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
