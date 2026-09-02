import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from pydantic import BaseModel
from typing import Deque, Dict, List, Optional

# Run as a plain script path (`python3 mesh/p2p/p2p_app.py`), not as a
# module (`python3 -m mesh.p2p.p2p_app`), and Python only puts this
# file's own directory on sys.path - not the repo root - so `import mesh`
# fails. Prepending the repo root here (three levels up: mesh/p2p/ ->
# mesh/ -> repo root) makes both invocation styles work, confirmed live
# this was a real trip-up (`ModuleNotFoundError: No module named 'mesh'`)
# the first time this script was actually run.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mesh.lib.agent_sdk import AdiyanAgent  # noqa: E402
from mesh.p2p import constants as p2p_constants  # noqa: E402

# community=None, always, for every ask() call this worker makes - this is
# the SERVING side of an offload (someone else's inference_router picked
# this machine), so it must run locally only and never re-offload, same
# reasoning mesh/compute_share/skills/run_inference.py's own docstring
# documents for its own peer-serving role.
_p2p_agent = AdiyanAgent('p2p_worker')

# Your live Render production route URL
RENDER_MATCHMAKER_URL = "https://matmaker.onrender.com"

# --- THE STRUCTURED CONTRACTS ---
class LLMRequestSchema(BaseModel):
    task_id: str
    prompt: str

class LLMResponseSchema(BaseModel):
    task_id: str
    status: str
    updated_response: str
    worker_node: str

def get_tailscale_static_ip() -> str:
    """
    Queries the local Tailscale daemon to fetch your stable, unique 100.x.y.z IP.
    """
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, check=True)
        stable_ip = result.stdout.strip()
        if stable_ip:
            return stable_ip
    except Exception:
        print("[!] Warning: Tailscale daemon not found or inactive. Falling back to localhost.")
    return "127.0.0.1"

# --- WORKER: BACKGROUND HEARTBEAT REGISTRATION LOOP ---
async def start_heartbeat_announcer(local_port: int, capabilities: List[str]):
    """
    Keeps the node alive on the Render matchmaker registry using the Tailscale IP.
    Matches the real server contract: POST /announce {public_ip, port, capabilities}.
    """
    tailscale_ip = get_tailscale_static_ip()
    print(f"[Heartbeat] Native Tailscale identity detected: {tailscale_ip}")

    while True:
        try:
            payload = json.dumps({
                "public_ip": tailscale_ip,
                "port": local_port,
                "capabilities": capabilities
            }).encode('utf-8')

            req = urllib.request.Request(
                f"{RENDER_MATCHMAKER_URL}/announce",
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            response = await asyncio.to_thread(urllib.request.urlopen, req)
            body = json.loads(response.read().decode('utf-8'))
            print(f"[Heartbeat] Checked in successfully - {body['total_active_peers']} active peer(s) on the registry.")
        except Exception as e:
            print(f"[Heartbeat Warning] Failed to register status: {e}")

        await asyncio.sleep(120) # Ping every 120s - server's REGISTRY_TIMEOUT is a fixed 300s, so this
                                  # halves heartbeat traffic vs. 60s while staying comfortably inside it

# --- GUARDRAILS ---
# The real protection this design has, given its own deliberate no-peer-
# auth stance (mesh/compute_share/README.md's original BitTorrent
# reasoning, carried over here - a genuinely different Adiyan install has
# no credential this one could check anyway). Verify what's being asked,
# not who's asking: a size limit and a per-source-IP request budget,
# both meaningful without any identity at all. See constants.py's own
# docstrings for each knob.
#
# Keyed on the UDP packet's own source IP - trivially spoofable by
# anyone motivated enough (unlike a real TCP handshake's source), but
# still real protection against an ordinary flood, which is the actual
# threat model here: someone hammering a discovered peer, not a
# resourced, deliberate spoofing attack. A determined attacker who
# spoofs source IPs also never sees the reply (UDP has no
# handshake to hijack), so spoofing gains them a nuisance, not a
# working exploit.
_recent_requests: Dict[str, Deque[float]] = defaultdict(deque)


def _rate_limited(source_ip: str) -> bool:
    now = time.monotonic()
    window = _recent_requests[source_ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= p2p_constants.RATE_LIMIT_PER_MINUTE:
        return True
    window.append(now)
    return False


# How many real ask() calls this worker is running right now - checked
# BEFORE accepting a task, not after, so a busy worker can say so
# immediately (see _handle_task's own comment) rather than a caller
# waiting out a long generation only to learn afterward this machine was
# never going to be free in time. Same reasoning
# mesh/inference_router/skills/complete.py's own LOCAL_CONCURRENCY_LIMIT
# documents, enforced on the serving side here instead of the asking side.
_in_flight = 0


async def _handle_task(data: bytes, peer_addr, sock: socket.socket) -> None:
    """One incoming packet's full handling, as its own asyncio task -
    NOT awaited inline by the receive loop below. That's the actual fix
    for busy-detection: if _handle_task were awaited directly in the
    loop, a second packet arriving mid-generation would just sit unread
    in the OS's own UDP buffer until this one finished - the sender
    would see a long silence indistinguishable from network trouble, not
    a clean, immediate BUSY reply. Firing this as a task lets the loop
    go straight back to recvfrom, so a busy reply can go out within
    milliseconds even while a real generation is still running for
    someone else."""
    global _in_flight
    source_ip = peer_addr[0]
    print(f"\n[Worker] Task request intercepted from: {peer_addr}")

    if _rate_limited(source_ip):
        print(f"[Worker] Rate limit exceeded for {source_ip!r} - dropped, no reply sent.")
        # No reply at all, not even a FAILED one - a rejection is still a
        # response an attacker can use to confirm the flood is landing;
        # silence gives them nothing to calibrate against.
        return

    try:
        payload = json.loads(data.decode('utf-8'))
        request_obj = LLMRequestSchema(**payload)
    except Exception as e:
        response_obj = LLMResponseSchema(task_id="ERR", status="FAILED", updated_response=str(e), worker_node="UNKNOWN")
        sock.sendto(response_obj.model_dump_json().encode('utf-8'), peer_addr)
        return

    if len(request_obj.prompt) > p2p_constants.MAX_PROMPT_CHARS:
        response_obj = LLMResponseSchema(
            task_id=request_obj.task_id, status="FAILED", worker_node="UNKNOWN",
            updated_response=(
                f'Prompt too long ({len(request_obj.prompt)} chars, '
                f'max {p2p_constants.MAX_PROMPT_CHARS}) - rejected before running it.'
            ),
        )
        sock.sendto(response_obj.model_dump_json().encode('utf-8'), peer_addr)
        return

    if _in_flight >= p2p_constants.WORKER_CONCURRENCY_LIMIT:
        # Answered instantly, without ever touching Ollama - this is the
        # signal discover_and_dispatch() uses to move on to the next
        # candidate instead of waiting out a peer that was never going
        # to be free in time.
        print(f"[Worker] Busy - declining task {request_obj.task_id!r}, telling the caller to try someone else.")
        response_obj = LLMResponseSchema(
            task_id=request_obj.task_id, status="BUSY",
            updated_response="This peer is already handling another request.",
            worker_node=get_tailscale_static_ip(),
        )
        sock.sendto(response_obj.model_dump_json().encode('utf-8'), peer_addr)
        return

    print(f"[Worker] Validated execution constraints for task: {request_obj.task_id}")
    _in_flight += 1
    try:
        # Real completion via the platform's own ask() - not a
        # placeholder. community=None (see _p2p_agent's own comment
        # above): always local, never re-offloaded from here.
        completion = await _p2p_agent.ask(request_obj.prompt, stage='p2p_worker', community=None)
        response_obj = LLMResponseSchema(
            task_id=request_obj.task_id, status="SUCCESS",
            updated_response=completion, worker_node=get_tailscale_static_ip(),
        )
    except Exception as e:
        response_obj = LLMResponseSchema(task_id="ERR", status="FAILED", updated_response=str(e), worker_node="UNKNOWN")
    finally:
        _in_flight -= 1

    sock.sendto(response_obj.model_dump_json().encode('utf-8'), peer_addr)


# --- WORKER: EXPOSED TARGET COMPUTE SOCKET ---
async def start_worker_endpoint(port: int, capabilities: List[str]):
    """
    Listens for direct incoming tasks sent securely through the Tailscale mesh.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Bind to 0.0.0.0 to listen across all interfaces (including Tailscale)
    sock.bind(("0.0.0.0", port))
    # Required for loop.sock_recvfrom() below - confirmed live this was
    # missing and was the real bug: a blocking-mode socket's recv can
    # starve the whole event loop, so the heartbeat task (scheduled via
    # create_task just below) never actually got a turn to run at all -
    # the worker printed its own startup line but never announced,
    # invisibly, with no error anywhere.
    sock.setblocking(False)

    # Start the background registration thread to check in with Render
    asyncio.create_task(start_heartbeat_announcer(port, capabilities))

    print(f"[Worker] Node engine live. Listening for tasks on port {port}...")
    loop = asyncio.get_running_loop()

    while True:
        data, peer_addr = await loop.sock_recvfrom(sock, 4096)
        # Fired as its own task, not awaited here - see _handle_task's
        # own docstring for why this is what makes an immediate BUSY
        # reply possible at all.
        asyncio.create_task(_handle_task(data, peer_addr, sock))

# --- SHARED: discover a peer via the matchmaker and dispatch one task,
# returning the answer directly (not printing it) - what
# mesh/inference_router/skills/complete.py's own _run_on_peer() calls
# instead of compute_share.offload, and what query_and_dispatch_task
# below now wraps for its own CLI/print-facing use. ---
async def discover_and_dispatch(target_model: str, prompt_text: str, timeout: float = 30.0) -> Optional[str]:
    # `target_model` is NOT used to filter discovery - real bug, confirmed
    # live: a worker always answers with whatever model its own local
    # ask() resolves (see start_worker_endpoint's own comment), completely
    # independent of the capability string it happened to register under
    # (the hardcoded placeholder list in server.py/this file's own main(),
    # 'qwen2.5-7b'/'llama3' - never a real model name like the caller
    # actually asked for, e.g. 'qwen3:8b-16k'). Filtering discover() by
    # target_model meant the REAL production path (inference_router's
    # complete.py, which always passes a real model name) could never
    # find a peer at all, ever - discovery came back empty every single
    # time, silently, which is why _run_on_peer kept returning None all
    # night despite peers being genuinely registered and reachable.
    # Manual CLI tests (`run_task qwen2.5-7b ...`) only ever worked
    # because that command happened to pass the matching placeholder
    # label by hand. No filter is the correct fix, not a different
    # filter value: any peer can serve any request, so any registered
    # peer is a valid candidate.
    try:
        url = f"{RENDER_MATCHMAKER_URL}/discover"
        response = await asyncio.to_thread(urllib.request.urlopen, url)
        data = json.loads(response.read().decode('utf-8'))
        # Excludes this instance's own registered entry - without a model
        # filter narrowing the candidate list, self-routing became a real
        # risk: the whole point of dispatch is reaching a genuinely
        # different machine, and a self-loop would silently look like a
        # successful offload (a real answer comes back) while never
        # actually leaving this machine.
        own_ip = get_tailscale_static_ip()
        workers = [w for w in data.get("peers", []) if w.get("ip") != own_ip]
    except Exception as e:
        print(f"[Client Error] Discovery failed: {e}")
        return None

    if not workers:
        return None

    # Tries each candidate in turn, moving on immediately when one says
    # BUSY (see _handle_task's own comment - that reply comes back within
    # milliseconds, without ever touching Ollama, specifically so this
    # loop doesn't have to wait out a peer that was never going to answer
    # in time) or doesn't answer at all within `timeout`. Sequential, not
    # a parallel race across every candidate at once (mesh/compute_share/
    # skills/offload.py's own RACE_CANDIDATES approach) - simpler, and
    # fine for the handful of peers this network actually has right now;
    # worth revisiting if the peer count ever grows enough that trying
    # them one at a time meaningfully adds up.
    for target_worker in workers:
        worker_ip, worker_port = target_worker["ip"], target_worker["port"]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", 0))
            sock.setblocking(False)  # required for loop.sock_recvfrom() below - see start_worker_endpoint's own comment
            payload = LLMRequestSchema(task_id="task_p2p_777", prompt=prompt_text).model_dump_json().encode('utf-8')
            sock.sendto(payload, (worker_ip, worker_port))

            loop = asyncio.get_running_loop()
            reply, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), timeout=timeout)
            res_data = json.loads(reply.decode('utf-8'))
            status = res_data.get('status')
            if status == 'SUCCESS':
                return res_data.get('updated_response')
            if status == 'BUSY':
                print(f"[Client] {worker_ip}:{worker_port} is busy - trying the next peer.")
                continue
            # FAILED (or any other/unexpected status) - not this peer's
            # fault to retry elsewhere necessarily, but there's nothing
            # more to do with it here either; try the next candidate
            # rather than giving up on the whole request.
            print(f"[Client] {worker_ip}:{worker_port} returned {status!r} - trying the next peer.")
        except Exception as e:
            print(f"[Client] {worker_ip}:{worker_port} unreachable ({e}) - trying the next peer.")
        finally:
            sock.close()

    return None


# --- CLIENT CALL ROUTINE (CLI-facing wrapper around discover_and_dispatch) ---
async def query_and_dispatch_task(target_model: str, prompt_text: str):
    print(f"[Client] Interrogating Render registry for active workers supporting: {target_model}")
    # 90s, not discover_and_dispatch()'s own 30s default - a real prompt's
    # generation time can genuinely exceed 30s (confirmed live, well over
    # a minute on a loaded machine), and this is the CLI's own one-shot
    # wait, not the mesh's internal offload path (which has its own
    # timeout tuned separately in inference_router/skills/complete.py).
    result = await discover_and_dispatch(target_model, prompt_text, timeout=90.0)
    if result is None:
        print("[Client] No active P2P workers found, or the task failed - see any error above.")
        return

    print(f"\n================================================")
    print(f"SUCCESS: DATA OUTPUT RETURNED TO CORRECT SOURCE:")
    print(f"Response Content: {result}")
    print("================================================\n")

# --- RUNNER MANAGEMENT CONTROLLER ---
async def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  Run as Worker Server: python p2p_app.py serve 9999")
        print("  Run as Client Caller:  python p2p_app.py run_task qwen2.5-7b \"your prompt here\"")
        print("                         (prompt defaults to a canned test string if omitted)")
        return

    action = sys.argv[1]
    if action == "serve":
        port = int(sys.argv[2])
        # Offer local model specs
        await start_worker_endpoint(port, capabilities=["qwen2.5-7b", "llama3"])
    elif action == "run_task":
        model = sys.argv[2]
        prompt_text = sys.argv[3] if len(sys.argv) > 3 else "Extract data schema fields."
        await query_and_dispatch_task(model, prompt_text)

if __name__ == "__main__":
    # Ensure you have your requirements met: pip install pydantic
    asyncio.run(main())
