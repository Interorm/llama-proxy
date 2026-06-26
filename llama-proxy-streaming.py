import asyncio, time, subprocess, httpx, socket
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import uvicorn
from typing import Literal

WINDOWS_MAC = 'A0:AD:9F:B1:E5:DB'
WINDOWS_IP = '192.168.42.42'
WINDOWS_USER = 'Karl'
LLAMA_PORT = '8080'

TIMEOUT = 60
TIMEOUT_COUNTER = 0
GAMING_THRESHOLD = 10

BOOT_LOCK: asyncio.Lock = None



app = FastAPI()
HTTP_CLIENT: httpx.AsyncClient = None
LAST_REQUEST_TIME = time.time()
PC_STATE: Literal['unknown', 'ready', 'starting', 'do-not-disturb', 'off'] = 'unknown'


def send_wol(): subprocess.run(["wakeonlan", WINDOWS_MAC])

async def ssh_run(cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        'ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
        f"{WINDOWS_USER}@{WINDOWS_IP}", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def start_llama(): await ssh_run('powershell -c net start LlamaServer')

async def stop_llama(): await ssh_run('powershell -c net stop LlamaServer')





async def shutdown_if_idle():
    global TIMEOUT_COUNTER
    global PC_STATE

    while True:
        await asyncio.sleep(60)
        if time.time() - LAST_REQUEST_TIME > TIMEOUT and PC_STATE == 'ready':
                if TIMEOUT_COUNTER >= 10: 
                    TIMEOUT_COUNTER = 0
                    PC_STATE = 'off'
                    await ssh_run("shutdown /s /t 60")
                else: 
                    TIMEOUT_COUNTER += 1
        else: TIMEOUT_COUNTER = 0


async def is_dnd_active() -> bool:
    try:
        result = await ssh_run('powershell -c "Test-Path C:\\Users\\Karl\\.llama-proxy\\llama-dnd.flag"')
        return result.strip().lower() == "true"
    except Exception:
        return False

async def check_avaibility():
    global PC_STATE

    while True:
        await asyncio.sleep(120)
        if not await is_pc_reachable():
            if PC_STATE != 'starting': PC_STATE = 'off'
            continue
        if await is_dnd_active():
            if PC_STATE == 'ready': await stop_llama()
            PC_STATE = 'do-not-disturb'
        elif PC_STATE == 'do-not-disturb':
            PC_STATE = 'unknown'


@app.on_event("startup")
async def startup():
    global BOOT_LOCK, HTTP_CLIENT
    BOOT_LOCK = asyncio.Lock()
    HTTP_CLIENT = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
    )
    asyncio.create_task(shutdown_if_idle())
    asyncio.create_task(check_avaibility())


@app.on_event("shutdown")
async def shutdown():
    await stop_llama()
    await HTTP_CLIENT.aclose()




async def is_pc_reachable() -> bool:
    proc = await asyncio.create_subprocess_exec(
        'ping', '-c', '1', '-W', '2', WINDOWS_IP,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    return await proc.wait() == 0

async def is_llama_running() -> bool:
    try:
        result = await HTTP_CLIENT.get(f"http://{WINDOWS_IP}:{LLAMA_PORT}/health", timeout=2)
        return result.status_code == 200
    except Exception:
        return False


async def ensure_inference_ready():
    global PC_STATE

    if PC_STATE == 'ready': return
    if PC_STATE == 'do-not-disturb': raise RuntimeError("PC is used by owner, please wait")


    async with BOOT_LOCK:
        if PC_STATE == 'ready': return
        if PC_STATE == 'do-not-disturb': raise RuntimeError("PC is used by owner, please wait")

        if not await is_pc_reachable():
            PC_STATE = 'starting'
            send_wol()
            for _ in range(60):
                await asyncio.sleep(2)
                if await is_pc_reachable():
                    break
            else:
                PC_STATE = 'off'
                raise RuntimeError("PC did not wake up in time.")
            await asyncio.sleep(5)


        if not await is_llama_running():
            PC_STATE = 'starting'
            await start_llama()
            for _ in range(30):
                await asyncio.sleep(2)
                try:
                    if is_llama_running(): 
                        PC_STATE = 'ready'
                        break
                except Exception: pass
            else: 
                PC_STATE = 'unknown'
        else: 
            PC_STATE = 'ready'




@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(request: Request, path: str):
    
    global LAST_REQUEST_TIME
    LAST_REQUEST_TIME = time.time()

    try:
        await ensure_inference_ready()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if PC_STATE != 'ready': raise HTTPException(status_code=503, detail="Unable to start llama-server!")


    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("accept-encoding", None)

    req = HTTP_CLIENT.build_request(
        method=request.method,
        url=f"http://{WINDOWS_IP}:{LLAMA_PORT}/{path}",
        headers=headers,
        content=body,
    )
    resp = await HTTP_CLIENT.send(
        req,
        stream=True,
    )

    async def body_iterator():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    response_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() not in (
            "content-length",
            "transfer-encoding",
            "connection",
        )
    }

    return StreamingResponse(
        body_iterator(),
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9090)