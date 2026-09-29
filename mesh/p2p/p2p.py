import asyncio
import json
import socket
import subprocess
import sys
import urllib.request
from pydantic import BaseModel
from typing import List

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
    """
    print(f"[Heartbeat] Native Tailscale identity detected: {tailscale_ip}")
    tailscale_ip = get_tailscale_static_ip()
    print(f"[Heartbeat] Native Tailscale identity detected: {tailscale_ip}")
    
    while True:
        try:
            payload = json.dumps({
                "ip": tailscale_ip,
                "port": local_port,
                "capabilities": capabilities
            }).encode('utf-8')
            
            req = urllib.request.Request(
                f"{RENDER_MATCHMAKER_URL}/announce", 
                data=payload, 
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            # Perform quick background async HTTP call
            await asyncio.to_thread(urllib.request.urlopen, req)
            print("[Heartbeat] Checked in successfully with Render cloud mesh.")
        except Exception as e:
            print(f"[Heartbeat Warning] Failed to register status: {e}")
            
        await asyncio.sleep(60) # Ping every 60 seconds

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
            
            # --- STRUCTURE-BOUND LLM PLACEHOLDER ---
            processed_text = f"Processed struct payload for task {request_obj.task_id} on pure Tailscale route."
            
            response_obj = LLMResponseSchema(
                task_id=request_obj.task_id, 
                status="SUCCESS", 
                updated_response=processed_text,
                worker_node=get_tailscale_static_ip()
            )
        except Exception as e:
            response_obj = LLMResponseSchema(
                task_id="ERR", status="FAILED", updated_response=str(e), worker_node="UNKNOWN"
            )
            
        sock.sendto(response_obj.model_dump_json().encode('utf-8'), peer_addr)

# --- CLIENT CALL ROUTINE ---
async def query_and_dispatch_task(target_model: str, prompt_text: str):
    """
    Consults Render to find a worker, then connects directly via their Tailscale static IP.
    """
    print(f"[Client] Interrogating Render registry for active workers supporting: {target_model}")
    try:
        url = f"{RENDER_MATCHMAKER_URL}/discover?model_required={target_model}"
        response = await asyncio.to_thread(urllib.request.urlopen, url)
        data = json.loads(response.read().decode('utf-8'))
        workers = data.get("target_workers", [])
        
        if not workers:
            print("[Client] No active P2P workers found on the network for that spec framework.")
            return
            
        # Target the first available worker node
        target_worker = workers[0]
        worker_ip, worker_port = target_worker["ip"], target_worker["port"]
        print(f"[Client] Peer discovered via Render -> {worker_ip}:{worker_port}. Sending payload...")
        
        # Setup temporary transmission socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0)) 
        
        # Dispatch structured payload
        payload = LLMRequestSchema(task_id="task_p2p_777", prompt=prompt_text).model_dump_json().encode('utf-8')
        sock.sendto(payload, (worker_ip, worker_port))
        
        # Listen for the computational response to route back
        loop = asyncio.get_running_loop()
        reply, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), timeout=5.0)
        res_data = json.loads(reply.decode('utf-8'))
        
        print(f"\n================================================")
        print(f"SUCCESS: DATA OUTPUT RETURNED TO CORRECT SOURCE:")
        print(f"Status: {res_data['status']}")
        print(f"Worker Identity: {res_data['worker_node']}")
        print(f"Response Content: {res_data['updated_response']}")
        print("================================================\n")
        
    except Exception as e:
        print(f"[Client Error] Core routing pipeline blocked: {e}")

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
