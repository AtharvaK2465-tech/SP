#!/usr/bin/env python3
"""SysWatch: cross-platform local system monitor.

Only psutil and the Python standard library are required. HTTP handlers never
sample the machine; a background sampler owns all psutil collection and swaps
an immutable-ish snapshot under a lock.
"""
from __future__ import annotations

import argparse, csv, io, json, os, platform, signal, socket, sys, threading, time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import psutil

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SERVER_PID = os.getpid()
INTERVAL = 1.0

SNAPSHOT_LOCK = threading.RLock()
SNAPSHOT: dict[str, Any] = {}
ALERT_LOCK = threading.RLock()
ALERTS: deque[dict[str, Any]] = deque(maxlen=100)
THRESHOLDS = {"cpu": 90.0, "memory": 90.0, "disk": 90.0}
PROC_CACHE: dict[int, psutil.Process] = {}
CLIENTS: set[Any] = set()
CLIENTS_LOCK = threading.Lock()


def safe(fn, default=None):
    try:
        return fn()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, PermissionError, OSError):
        return default
    except Exception:
        return default


def kb(n: Any) -> int:
    try: return int(n // 1024)
    except Exception: return 0


def iso(ts: Any) -> str | None:
    try: return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except Exception: return None


def os_info() -> dict[str, Any]:
    return {
        "name": platform.system(), "version": platform.version(),
        "release": platform.release(), "architecture": platform.machine(),
        "platform": platform.platform(), "python": platform.python_version(),
        "kernel": platform.release(), "hostname": socket.gethostname(),
    }

OSINFO = os_info()
IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"


def priority_for_process(p: psutil.Process) -> Any:
    if IS_WINDOWS:
        val = safe(p.nice, None)
        names = {psutil.REALTIME_PRIORITY_CLASS:"REALTIME", psutil.HIGH_PRIORITY_CLASS:"HIGH",
                 psutil.ABOVE_NORMAL_PRIORITY_CLASS:"ABOVE_NORMAL", psutil.NORMAL_PRIORITY_CLASS:"NORMAL",
                 psutil.BELOW_NORMAL_PRIORITY_CLASS:"BELOW_NORMAL", psutil.IDLE_PRIORITY_CLASS:"IDLE"}
        return names.get(val, str(val) if val is not None else "?")
    return safe(p.nice, "?")


def process_state(p: psutil.Process) -> str:
    s = safe(p.status, "?")
    return {psutil.STATUS_RUNNING:"R", psutil.STATUS_SLEEPING:"S", psutil.STATUS_DISK_SLEEP:"D",
            psutil.STATUS_STOPPED:"T", psutil.STATUS_TRACING_STOP:"T", psutil.STATUS_ZOMBIE:"Z",
            psutil.STATUS_IDLE:"I"}.get(s, "?")


def proc_cmdline(p: psutil.Process) -> str:
    x = safe(p.cmdline, []) or []
    return " ".join(str(v) for v in x) if x else (safe(p.name, "?") or "?")


def make_process(p: psutil.Process) -> dict[str, Any] | None:
    try:
        with p.oneshot():
            return {
                "pid": p.pid, "ppid": safe(p.ppid, 0), "name": safe(p.name, "?"),
                "user": safe(p.username, "?") or "?", "state": process_state(p),
                "cpu": round(float(safe(p.cpu_percent, 0.0) or 0.0), 1),
                "rss": kb(safe(lambda: p.memory_info().rss, 0)),
                "nice": priority_for_process(p),
                "threads": int(safe(p.num_threads, 0) or 0),
                "start": iso(safe(p.create_time, 0)),
                "cmdline": proc_cmdline(p),
            }
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, PermissionError, OSError):
        return None


def system_snapshot(prev: dict[str, Any] | None) -> dict[str, Any]:
    vm = safe(psutil.virtual_memory, None)
    sm = safe(psutil.swap_memory, None)
    cpu = safe(psutil.cpu_percent, 0.0)
    per_cpu = safe(lambda: psutil.cpu_percent(interval=None, percpu=True), []) or []
    freq = safe(psutil.cpu_freq, None)
    load = safe(os.getloadavg, None) if hasattr(os, "getloadavg") else None
    if load is None:
        load = safe(psutil.getloadavg, None)
    net = safe(lambda: psutil.net_io_counters(pernic=True), {}) or {}
    disks = safe(psutil.disk_io_counters, None)
    partitions = []
    for part in safe(psutil.disk_partitions, []) or []:
        usage = safe(lambda p=part: psutil.disk_usage(p.mountpoint), None)
        if usage:
            partitions.append({"device": part.device, "mount": part.mountpoint, "fstype": part.fstype,
                               "used": kb(usage.used), "total": kb(usage.total), "percent": usage.percent})
    processes = []
    current_pids=set()
    for p0 in psutil.process_iter():
        current_pids.add(p0.pid)
        p = PROC_CACHE.setdefault(p0.pid, p0)
        row = make_process(p)
        if row is not None: processes.append(row)
    for pid in list(PROC_CACHE):
        if pid not in current_pids: PROC_CACHE.pop(pid, None)
    processes.sort(key=lambda x: (x["cpu"], x["rss"]), reverse=True)

    prev_net = (prev or {}).get("netCounters", {})
    net_rates = []
    for name, c in net.items():
        old = prev_net.get(name, {})
        net_rates.append({"name": name, "rx": max(0, c.bytes_recv-old.get("rx", c.bytes_recv)),
                          "tx": max(0, c.bytes_sent-old.get("tx", c.bytes_sent))})
    prev_disk = (prev or {}).get("diskCounters", {})
    disk_rates = {"read":0,"write":0}
    if disks:
        old = prev_disk
        disk_rates = {"read": max(0, disks.read_bytes-old.get("read", disks.read_bytes)),
                      "write": max(0, disks.write_bytes-old.get("write", disks.write_bytes))}
    own = psutil.Process(SERVER_PID)
    own_cpu = safe(own.cpu_percent, 0.0) or 0.0
    own_mem = kb(safe(lambda: own.memory_info().rss, 0))
    used_pct = float(getattr(vm, "percent", 0.0)) if vm else 0.0
    swap_total = kb(getattr(sm, "total", 0)) if sm else 0
    swap_used = kb(getattr(sm, "used", 0)) if sm else 0
    now = time.time()
    snap = {
        "timestamp": now, "iso": iso(now), "os": OSINFO, "uptime": max(0, int(safe(psutil.boot_time, now) and now-safe(psutil.boot_time, now))),
        "load": list(load) if load else None, "cpu": {"total": round(float(cpu),1), "perCore":[round(float(x),1) for x in per_cpu],
          "count": safe(psutil.cpu_count, 0), "logical": safe(lambda: psutil.cpu_count(True), 0),
          "frequency": ({"current": getattr(freq,"current",0),"min":getattr(freq,"min",0),"max":getattr(freq,"max",0)} if freq else None)},
        "memory": {"total":kb(getattr(vm,"total",0)),"used":kb(getattr(vm,"used",0)),"available":kb(getattr(vm,"available",0)),"percent":used_pct},
        "swap": {"total":swap_total,"used":swap_used,"percent":float(getattr(sm,"percent",0)) if sm else 0},
        "processes": processes, "processCount": len(processes),
        "net": net_rates, "netCounters": {k:{"rx":v.bytes_recv,"tx":v.bytes_sent} for k,v in net.items()},
        "disk": {"partitions":partitions,"io":disk_rates},
        "diskCounters": ({"read":disks.read_bytes,"write":disks.write_bytes} if disks else {}),
        "monitor": {"pid":SERVER_PID,"cpu":float(own_cpu),"rss":own_mem},
        "thresholds": dict(THRESHOLDS),
    }
    return snap


def add_alert(kind: str, value: float, threshold: float):
    severity = "critical" if value >= threshold + 10 else "warning"
    with ALERT_LOCK:
        if ALERTS and ALERTS[-1]["kind"] == kind and time.time()-ALERTS[-1]["epoch"] < 10:
            return
        ALERTS.append({"timestamp":iso(time.time()),"epoch":time.time(),"kind":kind,"value":round(value,1),"threshold":threshold,"severity":severity})


def alert_check(snap: dict[str, Any]):
    checks=[("cpu",snap["cpu"]["total"]), ("memory",snap["memory"]["percent"]),
            ("disk",max([p["percent"] for p in snap["disk"]["partitions"]], default=0))]
    for k,v in checks:
        if v >= THRESHOLDS[k]: add_alert(k,v,THRESHOLDS[k])


def sampler():
    global SNAPSHOT
    prev = None
    safe(psutil.cpu_percent, None); safe(lambda: psutil.cpu_percent(interval=None, percpu=True), None)
    while True:
        started=time.monotonic()
        snap=system_snapshot(prev)
        prev=snap
        alert_check(snap)
        with SNAPSHOT_LOCK: SNAPSHOT=snap
        broadcast({"type":"snapshot","data":snap})
        elapsed=time.monotonic()-started
        time.sleep(max(0, INTERVAL-elapsed))


def json_bytes(obj): return json.dumps(obj, separators=(",",":"), ensure_ascii=False).encode()


def process_detail(pid: int) -> dict[str, Any]:
    p=psutil.Process(pid)
    def many(fn):
        try: return fn()
        except (psutil.AccessDenied,psutil.NoSuchProcess,psutil.ZombieProcess,PermissionError,OSError): return []
    detail={"pid":pid,"name":safe(p.name,"?"),"parent":None,"children":[],"cmdline":safe(p.cmdline,[]),
            "openFiles":many(p.open_files),"threads":many(p.threads),"memoryMaps":many(p.memory_maps),
            "connections":many(p.net_connections),"environment":safe(p.environ,{})}
    pp= safe(p.parent,None)
    if pp: detail["parent"]={"pid":pp.pid,"name":safe(pp.name,"?")}
    detail["children"]=[{"pid":c.pid,"name":safe(c.name,"?")} for c in many(p.children)]
    detail["openFiles"]= [{"path":x.path,"fd":x.fd} for x in detail["openFiles"]]
    detail["threads"]= [{"id":x.id,"user":x.user_time,"system":x.system_time} for x in detail["threads"]]
    detail["memoryMaps"]= [{"path":x.path,"rss":kb(x.rss),"private":kb(getattr(x,"private",0))} for x in detail["memoryMaps"]]
    detail["connections"]= [{"fd":getattr(x,"fd",-1),"family":str(x.family),"type":str(x.type),"local":list(x.laddr) if x.laddr else None,"remote":list(x.raddr) if x.raddr else None,"status":x.status} for x in detail["connections"]]
    return detail


def control_process(pid:int, action:str, value:Any=None):
    if pid <= 1 or pid == SERVER_PID: return False,"refusing to control protected server/system pid"
    try: p=psutil.Process(pid)
    except (psutil.NoSuchProcess,psutil.ZombieProcess): return False,"no such process"
    try:
        if action=="terminate": p.terminate()
        elif action=="kill": p.kill()
        elif action=="stop":
            if IS_WINDOWS: p.suspend()
            else: p.send_signal(signal.SIGSTOP)
        elif action=="continue":
            if IS_WINDOWS: p.resume()
            else: p.send_signal(signal.SIGCONT)
        else: return False,"unknown action"
        return True,"ok"
    except psutil.AccessDenied: return False,"permission denied (try administrator/sudo)"
    except psutil.NoSuchProcess: return False,"no such process"
    except Exception as e: return False,str(e)


def renice(pid:int, nice:int):
    if pid<=1 or pid==SERVER_PID: return False,"refusing to renice protected server/system pid"
    try:
        p=psutil.Process(pid)
        if IS_WINDOWS:
            mapping={-20:psutil.REALTIME_PRIORITY_CLASS,-10:psutil.HIGH_PRIORITY_CLASS,-5:psutil.ABOVE_NORMAL_PRIORITY_CLASS,
                     0:psutil.NORMAL_PRIORITY_CLASS,5:psutil.BELOW_NORMAL_PRIORITY_CLASS,19:psutil.IDLE_PRIORITY_CLASS}
            cls=min(mapping,key=lambda x:abs(x-nice)); p.nice(mapping[cls])
        else: p.nice(nice)
        return True,"ok"
    except psutil.AccessDenied: return False,"permission denied (try administrator/sudo)"
    except psutil.NoSuchProcess: return False,"no such process"
    except ValueError: return False,"invalid priority"
    except Exception as e: return False,str(e)


def connections():
    out=[]
    for c in safe(psutil.net_connections,"") or []:
        try:
            proto="tcp" if c.type==socket.SOCK_STREAM else "udp" if c.type==socket.SOCK_DGRAM else str(c.type)
            out.append({"protocol":proto,"local":list(c.laddr) if c.laddr else None,"remote":list(c.raddr) if c.raddr else None,"state":c.status,"pid":c.pid})
        except Exception: pass
    return out


def broadcast(obj):
    msg=json_bytes(obj)
    with CLIENTS_LOCK: clients=list(CLIENTS)
    for ws in clients:
        try: ws.send(msg)
        except Exception:
            with CLIENTS_LOCK: CLIENTS.discard(ws)

try:
    import websockets.sync.server as ws_server
except Exception:
    ws_server=None


def websocket_thread(port):
    if ws_server is None: return
    def handler(ws):
        with CLIENTS_LOCK: CLIENTS.add(ws)
        try:
            for _ in ws:
                pass
        except Exception: pass
        finally:
            with CLIENTS_LOCK: CLIENTS.discard(ws)
    try:
        with ws_server.serve(handler,"127.0.0.1",port+1):
            threading.Event().wait()
    except Exception: pass


class Handler(BaseHTTPRequestHandler):
    server_version="SysWatch/1.0"
    def log_message(self,*args): pass
    def local_only(self): return self.client_address[0] in ("127.0.0.1","::1")
    def send_json(self,obj,status=200,headers=None):
        b=json_bytes(obj); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.send_header("Cache-Control","no-store")
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if not self.local_only(): return self.send_json({"ok":False,"error":"localhost only"},403)
        u=urlparse(self.path); path=u.path
        if path=="/api/stats":
            with SNAPSHOT_LOCK: return self.send_json(SNAPSHOT)
        if path=="/api/alerts":
            with ALERT_LOCK: return self.send_json({"thresholds":dict(THRESHOLDS),"events":list(ALERTS)})
        if path=="/api/connections": return self.send_json({"connections":connections()})
        if path.startswith("/api/proc/"):
            try: pid=int(path.rsplit("/",1)[1]); return self.send_json(process_detail(pid))
            except (ValueError,psutil.NoSuchProcess): return self.send_json({"ok":False,"error":"no such process"},404)
            except psutil.AccessDenied: return self.send_json({"ok":False,"error":"not permitted"},403)
            except Exception as e: return self.send_json({"ok":False,"error":str(e)},500)
        if path=="/api/export":
            fmt=parse_qs(u.query).get("format",["json"])[0]
            with SNAPSHOT_LOCK: snap=dict(SNAPSHOT)
            if fmt=="json": return self.send_json(snap,headers={"Content-Disposition":"attachment; filename=syswatch.json"})
            if fmt=="csv":
                out=io.StringIO(); w=csv.writer(out); w.writerow(["pid","ppid","name","user","state","cpu_percent","rss_kb","priority","threads","start","cmdline"])
                for p in snap.get("processes",[]): w.writerow([p.get(k,"") for k in ["pid","ppid","name","user","state","cpu","rss","nice","threads","start","cmdline"]])
                b=out.getvalue().encode(); self.send_response(200); self.send_header("Content-Type","text/csv"); self.send_header("Content-Disposition","attachment; filename=syswatch.csv"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
            return self.send_json({"ok":False,"error":"format must be json or csv"},400)
        if path=="/" or path=="/index.html": return self.static(WEB/"index.html")
        return self.send_json({"ok":False,"error":"not found"},404)
    def static(self,p):
        try: b=p.read_bytes()
        except FileNotFoundError: return self.send_json({"ok":False,"error":"not found"},404)
        self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        if not self.local_only(): return self.send_json({"ok":False,"error":"localhost only"},403)
        u=urlparse(self.path); q=parse_qs(u.query)
        if u.path=="/api/kill":
            try: pid=int(q.get("pid",[""])[0]); sig=int(q.get("sig",["15"])[0])
            except ValueError: return self.send_json({"ok":False,"error":"invalid pid/sig"},400)
            actions={15:"terminate",9:"kill",19:"stop",18:"continue"}
            if sig not in actions: return self.send_json({"ok":False,"error":"sig must be 15, 9, 19, or 18"},400)
            ok,msg=control_process(pid,actions[sig]); return self.send_json({"ok":ok,"error":None if ok else msg},200 if ok else 400)
        if u.path=="/api/renice":
            try: pid=int(q.get("pid",[""])[0]); nice=int(q.get("nice",[""])[0])
            except ValueError: return self.send_json({"ok":False,"error":"invalid pid/nice"},400)
            if not -20<=nice<=19 and not IS_WINDOWS: return self.send_json({"ok":False,"error":"nice must be -20..19"},400)
            ok,msg=renice(pid,nice); return self.send_json({"ok":ok,"error":None if ok else msg},200 if ok else 400)
        if u.path=="/api/alerts":
            try:
                n=int(self.headers.get("Content-Length","0")); data=json.loads(self.rfile.read(n) or b"{}")
                for k in THRESHOLDS:
                    if k in data:
                        v=float(data[k]);
                        if not 0<=v<=100: raise ValueError
                        THRESHOLDS[k]=v
                return self.send_json({"ok":True,"thresholds":dict(THRESHOLDS)})
            except Exception: return self.send_json({"ok":False,"error":"thresholds must be numbers from 0 to 100"},400)
        return self.send_json({"ok":False,"error":"not found"},404)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("port",nargs="?",type=int,default=8080); args=ap.parse_args()
    if not 1024<=args.port<=65535: ap.error("port must be 1024..65535")
    t=threading.Thread(target=sampler,daemon=True); t.start()
    if ws_server:
        threading.Thread(target=websocket_thread,args=(args.port,),daemon=True).start()
    http=ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    print(f"SysWatch running at http://127.0.0.1:{args.port} ({OSINFO['name']})")
    try: http.serve_forever()
    except KeyboardInterrupt: pass
    finally: http.server_close()

if __name__=="__main__": main()
