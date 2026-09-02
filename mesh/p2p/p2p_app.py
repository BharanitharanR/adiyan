import asyncio
import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from pydantic import BaseModel
from typing import List, Optional

# Run as a plain script path (`python3 mesh/p2p/p2p_app.py`), not as a
# module (`python3 -m mesh.p2p.p2p_app`), and Python only puts this
# file's own directory on sys.path - not the repo root - so `import mesh`
# fails. Prepending the repo root here (three levels up: mesh/p2p/ ->
# mesh/ -> repo root) makes both invocation styles work, confirmed live
# this was a real trip-up (`ModuleNotFoundError: No module named 'mesh'`)
# the first time this script was actually run.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mesh.lib.agent_sdk import AdiyanAgent  # noqa: E402

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

# --- WORKER: EXPOSED TARGET COMPUTE SOCKET ---
async def start_worker_endpoint(port: int, capabilities: List[str]):
    """
    Listens for direct incoming tasks sent securely through the Tailscale mesh.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Bind to 0.0.0.0 to listen across all interfaces (including Tailscale)
    sock.bind(("0.0.0.0", port))

    # Start the background registration thread to check in with Render
    asyncio.create_task(start_heartbeat_announcer(port, capabilities))

    print(f"[Worker] Node engine live. Listening for tasks on port {port}...")
    loop = asyncio.get_running_loop()

    while True:
        data, peer_addr = await loop.sock_recvfrom(sock, 4096)
        print(f"\n[Worker] Task request intercepted from: {peer_addr}")

        try:
            payload = json.loads(data.decode('utf-8'))
            request_obj = LLMRequestSchema(**payload)
            print(f"[Worker] Validated execution constraints for task: {request_obj.task_id}")

            # Real completion via the platform's own ask() - not a
            # placeholder. community=None (see _p2p_agent's own comment
            # above): always local, never re-offloaded from here.
            completion = await _p2p_agent.ask(request_obj.prompt, stage='p2p_worker', community=None)

            response_obj = LLMResponseSchema(
                task_id=request_obj.task_id,
                status="SUCCESS",
                updated_response=completion,
                worker_node=get_tailscale_static_ip()
            )
        except Exception as e:
            response_obj = LLMResponseSchema(
                task_id="ERR", status="FAILED", updated_response=str(e), worker_node="UNKNOWN"
            )

        sock.sendto(response_obj.model_dump_json().encode('utf-8'), peer_addr)

# --- SHARED: discover a peer via the matchmaker and dispatch one task,
# returning the answer directly (not printing it) - what
# mesh/inference_router/skills/complete.py's own _run_on_peer() calls
# instead of compute_share.offload, and what query_and_dispatch_task
# below now wraps for its own CLI/print-facing use. ---
async def discover_and_dispatch(target_model: str, prompt_text: str, timeout: float = 30.0) -> Optional[str]:
    try:
        url = f"{RENDER_MATCHMAKER_URL}/discover?capability={target_model}"
        response = await asyncio.to_thread(urllib.request.urlopen, url)
        data = json.loads(response.read().decode('utf-8'))
        workers = data.get("peers", [])
        if not workers:
            return None

        target_worker = workers[0]
        worker_ip, worker_port = target_worker["ip"], target_worker["port"]

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        payload = LLMRequestSchema(task_id="task_p2p_777", prompt=prompt_text).model_dump_json().encode('utf-8')
        sock.sendto(payload, (worker_ip, worker_port))

        loop = asyncio.get_running_loop()
        reply, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), timeout=timeout)
        res_data = json.loads(reply.decode('utf-8'))
        if res_data.get('status') != 'SUCCESS':
            return None
        return res_data.get('updated_response')
    except Exception as e:
        print(f"[Client Error] Core routing pipeline blocked: {e}")
        return None


# --- CLIENT CALL ROUTINE (CLI-facing wrapper around discover_and_dispatch) ---
async def query_and_dispatch_task(target_model: str, prompt_text: str):
    print(f"[Client] Interrogating Render registry for active workers supporting: {target_model}")
    result = await discover_and_dispatch(target_model, prompt_text, timeout=5.0)
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
        print("  Run as Client Caller: python p2p_app.py run_task qwen2.5-7b")
        return

    action = sys.argv[1]
    if action == "serve":
        port = int(sys.argv[2])
        # Offer local model specs
        await start_worker_endpoint(port, capabilities=["qwen2.5-7b", "llama3"])
    elif action == "run_task":
        model = sys.argv[2]
        await query_and_dispatch_task(model, "Extract data schema fields.")

if __name__ == "__main__":
    # Ensure you have your requirements met: pip install pydantic
    asyncio.run(main())
