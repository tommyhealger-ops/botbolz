"""Local, owner-installed PC agent. It only executes a fixed allowlist of actions."""
from __future__ import annotations

import argparse, base64, hashlib, hmac, json, os, platform, socket, subprocess, sys, time, uuid
from pathlib import Path
from typing import Any

import httpx, psutil

STATE_FILE = Path(os.environ.get("AGENT_STATE_FILE", "agent-state.json"))
ALLOWED_APPS = {
    "notepad": {"win32": ["notepad.exe"], "linux": ["gedit"], "darwin": ["open", "-a", "TextEdit"]},
    "calculator": {"win32": ["calc.exe"], "linux": ["gnome-calculator"], "darwin": ["open", "-a", "Calculator"]},
}

def load_state() -> dict[str, str]:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
def save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True); STATE_FILE.write_text(json.dumps(state), encoding="utf-8"); os.chmod(STATE_FILE, 0o600)
def os_key() -> str: return {"Windows":"win32","Darwin":"darwin"}.get(platform.system(),"linux")
def metrics() -> dict[str, Any]:
    vm=psutil.virtual_memory(); disks=[]
    for p in psutil.disk_partitions(all=False):
        try:
            u=psutil.disk_usage(p.mountpoint); disks.append({"mount":p.mountpoint,"free_gb":round(u.free/2**30,1),"used_gb":round(u.used/2**30,1),"total_gb":round(u.total/2**30,1)})
        except OSError: pass
    return {"cpu_percent":psutil.cpu_percent(interval=0.1),"ram_used_gb":round((vm.total-vm.available)/2**30,1),"ram_total_gb":round(vm.total/2**30,1),"disks":disks,"local_ip":local_ip(),"uptime":fmt_duration(time.time()-psutil.boot_time())}
def local_ip() -> str:
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("8.8.8.8",80)); ip=s.getsockname()[0];s.close();return ip
    except OSError:return "unknown"
def fmt_duration(sec: float) -> str:
    sec=int(sec); return f"{sec//86400}d {(sec%86400)//3600}h {(sec%3600)//60}m"
def signed_headers(secret: str, body: bytes) -> dict[str,str]:
    stamp=str(int(time.time())); sig=hmac.new(secret.encode(),stamp.encode()+b"."+body,hashlib.sha256).hexdigest(); return {"X-Agent-Timestamp":stamp,"X-Agent-Signature":sig}
def request(client: httpx.Client, method: str, url: str, state: dict[str,str], payload: dict|None=None) -> dict:
    raw=json.dumps(payload or {},separators=(",",":")).encode(); r=client.request(method,url,content=raw,headers={**signed_headers(state["secret"],raw),"Content-Type":"application/json"});r.raise_for_status();return r.json()
def do_command(command: dict) -> dict[str, Any]:
    action=command["action"]; payload=command.get("payload",{})
    if action=="screenshot":
        import mss
        out=Path("screenshots");out.mkdir(exist_ok=True); path=out/f"screen-{int(time.time())}.png"
        with mss.mss() as sct:sct.shot(output=str(path))
        # The image is returned only over the authenticated HTTPS server channel.
        return {"saved_locally":str(path),"screenshot_b64":base64.b64encode(path.read_bytes()).decode()}
    if action=="lock":
        cmds={"win32":["rundll32.exe","user32.dll,LockWorkStation"],"linux":["loginctl","lock-session"],"darwin":["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession","-suspend"]}; subprocess.run(cmds[os_key()],check=True);return {}
    if action in {"shutdown","restart"}:
        cmds={"win32":["shutdown","/s" if action=="shutdown" else "/r","/t","15"],"linux":["systemctl","poweroff" if action=="shutdown" else "reboot"],"darwin":["osascript","-e",'tell app "System Events" to shut down' if action=="shutdown" else 'tell app "System Events" to restart']}; subprocess.run(cmds[os_key()],check=True);return {"scheduled":True}
    if action=="list_apps": return {"apps":[p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]][:200]}
    if action in {"launch_app","close_app"}:
        name=payload.get("name")
        if name not in ALLOWED_APPS: raise ValueError("Application is not allowlisted")
        if action=="launch_app": subprocess.Popen(ALLOWED_APPS[name][os_key()]); return {"launched":name}
        killed=0
        for p in psutil.process_iter(["name"]):
            if p.info["name"] and p.info["name"].lower().startswith(name): p.terminate();killed+=1
        return {"closed":name,"count":killed}
    raise ValueError("Unsupported action")
def pair(server: str, code: str) -> None:
    state=load_state(); device_id=state.get("device_id",str(uuid.uuid4()))
    data={"code":code,"device_id":device_id,"name":socket.gethostname(),"os":f"{platform.system()} {platform.release()}"}
    with httpx.Client(timeout=15) as c:r=c.post(server+"/api/agent/pair",json=data);r.raise_for_status();state.update(device_id=device_id,secret=r.json()["device_secret"],server=server);save_state(state)
    print(f"Paired {device_id}")
def run() -> None:
    state=load_state()
    if not {"device_id","secret","server"}<=state.keys(): raise SystemExit("Agent is not paired. Run: python agent.py pair --code CODE")
    with httpx.Client(timeout=15) as client:
        first_heartbeat = True
        while True:
            try:
                base=f'{state["server"]}/api/agent/{state["device_id"]}'
                sample=metrics()
                if first_heartbeat: sample["agent_started"]=True; first_heartbeat=False
                request(client,"POST",base+"/heartbeat",state,{"metrics":sample})
                cmd=request(client,"GET",base+"/commands",state)
                if cmd.get("command"):
                    c=cmd["command"]
                    try: result={"command_id":c["id"],"ok":True,"result":do_command(c)}
                    except Exception as exc: result={"command_id":c["id"],"ok":False,"result":{"error":str(exc)}}
                    request(client,"POST",base+"/result",state,result)
                time.sleep(5)
            except (httpx.HTTPError,OSError) as exc: print(f"Connection unavailable, retrying: {exc}",file=sys.stderr);time.sleep(10)
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True); q=sub.add_parser("pair");q.add_argument("--server",required=True);q.add_argument("--code",required=True);sub.add_parser("run");a=p.parse_args()
    if a.command=="pair": pair(a.server.rstrip("/"),a.code)
    else: run()
if __name__=="__main__":main()
