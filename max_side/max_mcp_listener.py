r"""
3ds Max 2025 ici MCP dinleyicisi (pymxs, Python 3.11).

Calisma sekli
-------------
* 127.0.0.1:30025'te (MAX_MCP_PORT ile degisir) TCP dinler. Protokol: satir
  basina bir JSON istek, satir basina bir JSON yanit.
    istek : {"id": 1, "cmd": "info", "args": {...}, "timeout": 60, "key": "..."}
    yanit : {"id": 1, "ok": true, "result": ...}
            {"id": 1, "ok": false, "error": "..."}
* Soket kabulu ve okuma AYRI thread'lerde; pymxs cagrilari ise HER ZAMAN ana
  (UI) thread'de calisir. Istekler bir kuyruga girer; ana thread'deki
  "pump" (PySide6 QTimer, 50 ms) kuyrugu bosaltir ve sonucu isteyen thread'e
  bir Event ile geri verir. Bekleyen thread zaman asiminda hata doner, Max
  kilitlenmez (uzun is yine de UI'yi mesgul eder, bkz. README "Riskler").
* 3dsmaxbatch'te Qt olay dongusu bizim betigimiz calisirken donmez; bu yuzden
  test icin serve_blocking() var: ana thread'de pump'i kendimiz cagiririz.
* Bu modul SAHNEYI ASLA KAYDETMEZ. Kaydetme komutu yoktur.

Kullanim (Max icinde):
    import max_mcp_listener
    max_mcp_listener.start()          # QTimer pump ile dinlemeye basla
    max_mcp_listener.stop()
"""

import contextlib
import io
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback

# Ortak ayar modulu (stdlib): repo koku sys.path'te olmali (max_mcp_start.ms ekler).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from max_mcp import config as mcfg  # noqa: E402

HOST = mcfg.DEFAULTS["host"]
PORT = mcfg.DEFAULTS["port"]
DEFAULT_TIMEOUT = mcfg.DEFAULTS["timeout"]
PUMP_INTERVAL_MS = mcfg.DEFAULTS["pump_interval_ms"]
VERSION = "1.1.0"
LOG_PREFIX = "[MaxMcp] "


def log(msg):
    try:
        sys.stdout.write(LOG_PREFIX + str(msg) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Is kuyrugu
# --------------------------------------------------------------------------

class Job:
    __slots__ = ("id", "cmd", "args", "client", "event", "result", "error", "cancelled",
                 "created", "started")

    def __init__(self, jid, cmd, args, client=""):
        self.id = jid
        self.cmd = cmd
        self.args = args or {}
        self.client = client or ""
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.cancelled = False
        self.created = time.time()
        self.started = None


class Listener:
    """Soket + kuyruk. handlers: {cmd: fn(args) -> json'lanabilir}.
    pump() ana thread'den cagrilmali."""

    def __init__(self, handlers, host=HOST, port=PORT, key="",
                 default_timeout=DEFAULT_TIMEOUT):
        self.handlers = handlers
        self.host = host
        self.port = port
        self.key = key or ""
        self.default_timeout = default_timeout
        self.queue = queue.Queue()
        self.sock = None
        self.accept_thread = None
        self.running = False
        self.stop_requested = False  # serve_blocking icin
        self.main_thread = None
        self.started_at = None
        # executor(job) -> result; Max tarafi bunu gecmis/undo sarmalayicisiyla degistirir
        self.executor = lambda job: self.handlers[job.cmd](job.args)
        self.stats = {"handled": 0, "errors": 0, "timeouts": 0, "requests": 0}

    # --- soket tarafi (worker thread'ler) --------------------------------
    def start(self):
        if self.running:
            return
        self.main_thread = threading.current_thread()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self.sock = s
        self.running = True
        self.started_at = time.time()
        self.accept_thread = threading.Thread(
            target=self._accept_loop, name="MaxMcpAccept", daemon=True)
        self.accept_thread.start()
        log("dinleniyor %s:%d (anahtar: %s)" % (
            self.host, self.port, "acik" if self.key else "kapali"))

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        log("durduruldu")

    def _accept_loop(self):
        while self.running:
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(conn,),
                             name="MaxMcpClient", daemon=True).start()

    def _client_loop(self, conn):
        conn.settimeout(None)
        buf = b""
        try:
            while self.running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    reply = self.handle_line(line.decode("utf-8", "replace"))
                    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n")
                                 .encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # --- istek isleme (worker thread) -------------------------------------
    def handle_line(self, line):
        """Bir JSON satirini isler; kuyruga koyar, ana thread'i bekler."""
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"id": None, "ok": False, "error": "gecersiz JSON: %s" % exc}
        if not isinstance(req, dict):
            return {"id": None, "ok": False, "error": "istek nesne olmali"}
        jid = req.get("id")
        self.stats["requests"] += 1
        if self.key and req.get("key") != self.key:
            return {"id": jid, "ok": False, "error": "anahtar uyusmuyor"}
        cmd = req.get("cmd")
        if cmd == "ping":  # ana thread gerektirmez
            return {"id": jid, "ok": True, "result": {"pong": True,
                                                        "queue": self.queue.qsize()}}
        if cmd == "_stop":  # yalniz test/batch: blocking donguyu bitir
            self.stop_requested = True
            return {"id": jid, "ok": True, "result": "stop istendi"}
        if cmd not in self.handlers:
            return {"id": jid, "ok": False, "error": "bilinmeyen komut: %s" % cmd}
        try:
            timeout = float(req.get("timeout") or self.default_timeout)
        except (TypeError, ValueError):
            timeout = self.default_timeout
        job = Job(jid, cmd, req.get("args") or {}, str(req.get("client") or ""))
        self.queue.put(job)
        if not job.event.wait(timeout):
            self.stats["timeouts"] += 1
            if job.started is None:
                job.cancelled = True
                where = "kuyrukta bekliyordu, iptal edildi (Max mesgul ya da pump calismiyor)"
            else:
                where = "ana thread'de hala calisiyor, sonucu atilacak"
            return {"id": jid, "ok": False,
                    "error": "zaman asimi (%.0f sn): %s" % (timeout, where)}
        if job.error is not None:
            return {"id": jid, "ok": False, "error": job.error}
        return {"id": jid, "ok": True, "result": job.result}

    # --- ana thread tarafi --------------------------------------------------
    def pump(self, max_jobs=50):
        """Kuyruktaki isleri ana thread'de calistirir. QTimer ya da
        serve_blocking tarafindan cagrilir."""
        n = 0
        while n < max_jobs:
            try:
                job = self.queue.get_nowait()
            except queue.Empty:
                return n
            n += 1
            if job.cancelled:
                continue
            job.started = time.time()
            try:
                job.result = self.executor(job)
                self.stats["handled"] += 1
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                job.error = "%s: %s" % (type(exc).__name__, exc)
                tb = traceback.format_exc()
                if job.cmd in ("run_python",):
                    job.error += "\n" + tb
            finally:
                job.event.set()
        return n

    def serve_blocking(self, seconds=None, idle_sleep=0.02):
        """Batch/test: ana thread'i burada tutup pump'i kendimiz cagiririz.
        '_stop' komutu ya da sure dolunca doner."""
        self.stop_requested = False
        t_end = None if seconds is None else time.time() + seconds
        while not self.stop_requested:
            if self.pump() == 0:
                time.sleep(idle_sleep)
            if t_end is not None and time.time() > t_end:
                break
        return self.stats


# --------------------------------------------------------------------------
# pymxs komut isleyicileri (yalniz ana thread'de cagrilir)
# --------------------------------------------------------------------------

def _rt():
    import pymxs  # noqa: WPS433 (Max disinda yok)
    return pymxs, pymxs.runtime


@contextlib.contextmanager
def _undo_block(rt, label):
    """Adli undo kaydi. pymxs cagrilari varsayilan olarak undo KAPALI
    calisir: dogrudan theHold.Begin/Accept kullaninca kayit bos kaliyor
    (probe: getUndoNames bos). pymxs.undo(True, label) undo'yu acip adli
    kaydi olusturur ama istisnalari yutar; bu yuzden istisnayi blok icinde
    yakalayip blok kapandiktan sonra yeniden firlatiyoruz ve yarim kalan
    kaydi (yiginin tepesindeyse) 'max undo' ile geri sariyoruz."""
    import pymxs
    err = None
    try:
        depth_before = len(rt.theHold.getUndoNames())
    except Exception:  # noqa: BLE001
        depth_before = 0
    # SuperBegin/SuperAccept: komut icinde kendi kaydini kapatan islemler
    # (ProBoolean, bazi modifier'lar) pymxs.undo kaydini boluyordu; etiket
    # kayboluyor, sonraki islemler kayda hic girmiyordu (26.10 canli test).
    # Super blok hepsini tek "MCP: ..." seviyesinde toplar.
    super_on = False
    try:
        rt.theHold.SuperBegin()
        super_on = True
    except Exception:  # noqa: BLE001
        pass
    try:
        with pymxs.undo(True, label):
            try:
                yield
            except BaseException as exc:  # noqa: BLE001
                err = exc
    finally:
        if super_on:
            try:
                rt.theHold.SuperAccept(label)
            except Exception:  # noqa: BLE001
                pass
    if err is not None:
        try:
            if len(rt.theHold.getUndoNames()) > depth_before:
                rt.execute("max undo")
        except Exception:  # noqa: BLE001
            pass
        raise err


def _s(v):
    """pymxs degerini string'e cevir."""
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return repr(v)


def _p3(v):
    try:
        return [round(float(v.x), 4), round(float(v.y), 4), round(float(v.z), 4)]
    except Exception:  # noqa: BLE001
        return None


def _node_info(rt, n, detail=False):
    d = {
        "name": _s(n.name),
        "class": _s(rt.classOf(n)),
        "layer": _s(n.layer.name) if n.layer else None,
        "pos": _p3(n.pos),
        "hidden": bool(n.isHidden),
    }
    if detail:
        d.update({
            "superclass": _s(rt.superClassOf(n)),
            "bbox_min": _p3(n.min),
            "bbox_max": _p3(n.max),
            "rotation": _s(n.rotation),
            "scale": _p3(n.scale),
            "parent": _s(n.parent.name) if n.parent else None,
            "children": int(n.children.count),
            "frozen": bool(n.isFrozen),
            "selected": bool(n.isSelected),
            "wirecolor": _s(n.wirecolor),
            "material": None,
            "user_props": _s(rt.getUserPropBuffer(n)).strip(),
        })
        try:
            m = n.material
            if m is not None:
                d["material"] = {"name": _s(m.name), "class": _s(rt.classOf(m))}
        except Exception:  # noqa: BLE001
            pass
        try:
            d["faces"] = int(rt.getNumFaces(n))  # mesh'e cevrilemeyenlerde hata -> None
        except Exception:  # noqa: BLE001
            d["faces"] = None
    return d


def h_info(_args):
    pymxs, rt = _rt()
    ver = rt.maxVersion()
    ver_list = []
    try:
        for x in ver:
            try:
                ver_list.append(int(x))
            except (TypeError, ValueError):
                ver_list.append(_s(x))
    except Exception:  # noqa: BLE001
        ver_list = _s(ver)
    try:
        renderer = _s(rt.classOf(rt.renderers.current))
    except Exception:  # noqa: BLE001
        renderer = None
    return {
        "max_version": ver_list,
        "scene_path": _s(rt.maxFilePath),
        "scene_file": _s(rt.maxFileName),
        "dirty": bool(rt.getSaveRequired()),
        "units_system": _s(rt.units.SystemType),
        "units_display": _s(rt.units.DisplayType),
        "object_count": int(rt.objects.count),
        "selection_count": int(rt.selection.count),
        "current_time": _s(rt.currentTime),
        "anim_range": _s(rt.animationRange),
        "renderer": renderer,
        "quiet_mode": bool(rt.GetQuietMode()),
        "listener": status(),
    }


def h_selection(args):
    _pymxs, rt = _rt()
    limit = int(args.get("limit") or 200)
    detail = bool(args.get("detail", False))
    out = []
    for n in rt.selection:
        out.append(_node_info(rt, n, detail))
        if len(out) >= limit:
            break
    return {"count": int(rt.selection.count), "objects": out}


def h_query_objects(args):
    _pymxs, rt = _rt()
    pattern = args.get("name_pattern") or ""
    cls = (args.get("class_name") or "").lower()
    layer = (args.get("layer") or "").lower()
    sel_only = bool(args.get("selected_only", False))
    detail = bool(args.get("detail", False))
    limit = int(args.get("limit") or 100)
    src = rt.selection if sel_only else rt.objects
    out = []
    total = 0
    for n in src:
        if pattern and not rt.matchPattern(n.name, pattern=pattern, ignoreCase=True):
            continue
        if cls and _s(rt.classOf(n)).lower() != cls:
            continue
        if layer and (not n.layer or _s(n.layer.name).lower() != layer):
            continue
        total += 1
        if len(out) < limit:
            out.append(_node_info(rt, n, detail))
    return {"matched": total, "returned": len(out), "objects": out}


def h_layers(_args):
    _pymxs, rt = _rt()
    counts = {}
    for n in rt.objects:
        ln = _s(n.layer.name) if n.layer else ""
        counts[ln] = counts.get(ln, 0) + 1
    out = []
    lm = rt.layerManager
    for i in range(int(lm.count)):
        ly = lm.getLayer(i)
        name = _s(ly.name)
        out.append({
            "index": i,
            "name": name,
            "hidden": bool(ly.isHidden),
            "frozen": bool(ly.isFrozen),
            "current": bool(ly.current),
            "on": bool(ly.on),
            "objects": counts.get(name, 0),
        })
    return {"count": len(out), "layers": out}


def h_set_layer_hidden(args):
    pymxs, rt = _rt()
    name = args.get("name")
    hidden = bool(args.get("hidden", True))
    ly = rt.layerManager.getLayerFromName(name)
    if ly is None:
        raise ValueError("katman yok: %s" % name)
    ly.isHidden = hidden  # undo kaydini executor acar ("MCP: set_layer_hidden #id")
    try:
        rt.redrawViews()
    except Exception:  # noqa: BLE001
        pass
    return {"name": _s(ly.name), "hidden": bool(ly.isHidden)}


def h_viewport_capture(args):
    _pymxs, rt = _rt()
    path = args.get("path")
    if not path:
        d = os.path.join(os.environ.get("TEMP", "."), "max-mcp")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "capture_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    max_size = int(args.get("max_size") or 0)
    dib = rt.gw.getViewportDib()
    w, h = int(dib.width), int(dib.height)
    if max_size and max(w, h) > max_size:
        k = max_size / float(max(w, h))
        small = rt.bitmap(max(1, int(w * k)), max(1, int(h * k)))
        rt.copy(dib, small)
        rt.close(dib)
        dib = small
        w, h = int(dib.width), int(dib.height)
    dib.filename = path
    rt.save(dib)
    rt.close(dib)
    return {"path": path, "width": w, "height": h,
            "viewport": _s(rt.viewport.getType())}


def h_run_maxscript(args):
    pymxs, rt = _rt()
    code = args.get("code") or ""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        val = rt.execute(code)  # undo kaydi: executor (args['undo'] False ise acilmaz)
    return {"result": _s(val), "output": buf.getvalue()}


def h_run_python(args):
    pymxs, rt = _rt()
    code = args.get("code") or ""
    ns = {"pymxs": pymxs, "rt": rt, "__name__": "__max_mcp__"}
    buf = io.StringIO()
    result = None

    def _exec():
        nonlocal result
        try:
            compiled = compile(code, "<max_mcp>", "eval")
        except SyntaxError:
            compiled = None
        if compiled is not None:
            result = eval(compiled, ns)  # noqa: S307 (bilerek: uzak kod araci)
        else:
            exec(compile(code, "<max_mcp>", "exec"), ns)  # noqa: S102
            result = ns.get("result")

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        _exec()  # undo kaydi: executor
    try:
        json.dumps(result)
        res = result
    except (TypeError, ValueError):
        res = _s(result)
    return {"result": res, "output": buf.getvalue()}


HANDLERS = {
    "info": h_info,
    "selection": h_selection,
    "query_objects": h_query_objects,
    "layers": h_layers,
    "set_layer_hidden": h_set_layer_hidden,
    "viewport_capture": h_viewport_capture,
    "run_maxscript": h_run_maxscript,
    "run_python": h_run_python,
}


# --------------------------------------------------------------------------
# Gecmis / undo / kontrol noktasi (mantik: max_mcp_history.py; burasi pymxs)
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import max_mcp_history as mhist  # noqa: E402

_hist = {"store": None, "tracker": None, "cp": None, "pending": None}


def _undo_names():
    """theHold.getUndoNames(), en yeni ONCE olacak sekilde. Sira, yigindaki
    MCP etiketlerinin id'lerinden dogrulanir (probe'da tek kayit vardi)."""
    _p, rt = _rt()
    names = [str(n) for n in rt.theHold.getUndoNames()]
    ids = [mhist.parse_undo_label(n)[1] for n in names if mhist.parse_undo_label(n)]
    if len(ids) >= 2 and ids[0] < ids[-1]:  # en eski basta geliyorsa cevir
        names.reverse()
    return names


def _do_undo():
    _p, rt = _rt()
    rt.execute("max undo")


def _history_setup(cfg):
    base = mcfg.history_dir(cfg)
    _hist["store"] = mhist.HistoryStore(base / "history.jsonl",
                                        max_bytes=int(cfg.get("history_max_mb", 5)) * 1024 * 1024)
    _hist["tracker"] = mhist.UndoTracker(_undo_names, _do_undo,
                                         max_levels=lambda: int(_rt()[1].theHold.getMaxUndoLevels()))
    _hist["cp"] = mhist.CheckpointManager(base / "checkpoints",
                                          max_count=int(cfg.get("checkpoint_max_count", 10)),
                                          max_bytes=int(cfg.get("checkpoint_max_mb", 2048)) * 1024 * 1024)


def _snapshot():
    """Ucuz sahne anlik goruntusu: nesne adlari, secim, put sayaci."""
    _p, rt = _rt()
    try:
        return {"objects": [str(n.name) for n in rt.objects],
                "selection": [str(n.name) for n in rt.selection],
                "put_count": int(rt.theHold.GetGlobalPutCount())}
    except Exception:  # noqa: BLE001
        return {"objects": [], "selection": [], "put_count": None}


def _save_checkpoint(entry_id, tool):
    """Sahnenin kopyasi: saveMaxFile useNewFile:false -> maxFileName/maxFilePath
    DEGISMEZ (probe ile dogrulandi). Kaydetme dirty bayragini temizledigi icin
    geri setSaveRequired true yapilir."""
    _p, rt = _rt()
    cm = _hist["cp"]
    cm.dir.mkdir(parents=True, exist_ok=True)
    path = cm.path_for(entry_id, tool)
    was_dirty = bool(rt.getSaveRequired())
    ok = rt.saveMaxFile(str(path), useNewFile=False, quiet=True)
    if was_dirty:
        try:
            rt.setSaveRequired(True)
        except Exception:  # noqa: BLE001
            pass
    if not ok:
        raise RuntimeError("checkpoint save failed: %s" % path)
    cm.prune()
    return str(path)


def _reconcile_pending(keep_if_unchanged=False):
    """Undo seviyesi, pump bir python.Execute icinde kosuyorsa (dotNet yedek,
    batch) ancak o cagri BITINCE olusur; canli QTimer'da ise hemen gorunur.
    Bu yuzden degistiren komut 'beklemede' birakilir ve bir sonraki girişte
    yigina bakilir: seviye olustuysa bizim kaydimiz olarak onaylanir; komut
    hata verdiyse ve seviye hala tepedeyse geri sarilir. keep_if_unchanged:
    komutun hemen ardindan cagrilir; seviye henuz yoksa beklemede kalir."""
    p = _hist.get("pending")
    if not p:
        return
    try:
        names = _undo_names()
    except Exception:  # noqa: BLE001
        return
    grown = len(names) - p["depth"]
    if grown <= 0:
        if not keep_if_unchanged:
            _hist["pending"] = None  # sahne degismedi, seviye yok
        return
    _hist["pending"] = None
    pos = grown - 1  # bizim seviye (ustune kullanici eklediyse daha derinde)
    if p["ok"]:
        _hist["tracker"].accepted(p["id"], names[pos], p["depth"] + 1)
    elif pos == 0:
        try:
            _do_undo()  # yarim kalan degisiklik geri alindi
            log("#%d hata vermisti, yarim seviye geri alindi" % p["id"])
        except Exception:  # noqa: BLE001
            pass


def execute_recorded(cmd, args, client="", cfg=None):
    """Bir komutu gecmise yazarak calistirir: oncesi/sonrasi anlik goruntu,
    istege bagli kontrol noktasi, degistiren komutlarda etiketli undo kaydi."""
    if _hist["store"] is None:
        _history_setup(cfg or _state.get("cfg") or current_config())
    cfg = cfg or _state.get("cfg") or current_config()
    store, tracker, cm = _hist["store"], _hist["tracker"], _hist["cp"]
    _p, rt = _rt()
    _reconcile_pending()
    entry_id = store.next_id()
    changing = mhist.is_changing(cmd, args)
    label = mhist.undo_label(cmd, entry_id) if changing else None
    meta = cmd in mhist.META_COMMANDS
    started = time.time()
    before = None if meta else _snapshot()
    checkpoint = None
    result = error = None
    ok = True
    depth_before = None
    try:
        if not meta and cm.should_save(cfg.get("checkpoint_mode", "off"), changing):
            checkpoint = _save_checkpoint(entry_id, cmd)
        if changing:
            depth_before = len(_undo_names())
            with _undo_block(rt, label):
                result = HANDLERS[cmd](args)
        else:
            result = HANDLERS[cmd](args)
    except Exception as exc:  # noqa: BLE001
        ok = False
        error = "%s: %s" % (type(exc).__name__, exc)
        raise
    finally:
        if changing and depth_before is not None:
            _hist["pending"] = {"id": entry_id, "depth": depth_before, "ok": ok}
            _reconcile_pending(keep_if_unchanged=True)  # canli modda seviye hemen var
        finished = time.time()
        after = None if meta else _snapshot()
        entry = mhist.make_entry(entry_id, cmd, args, client, started, finished, ok,
                                 result=result, error=error, changing=changing and ok,
                                 undo_label_=label if (ok and changing) else None, before=before, after=after,
                                 checkpoint=checkpoint,
                                 put_count=after.get("put_count") if after else None)
        if cmd == "undo" and ok and isinstance(result, dict):
            entry["undone_ids"] = result.get("undone_ids", [])
        try:
            store.append(entry)
        except OSError as exc:
            log("gecmis yazilamadi: %s" % exc)
    return result


def _recorded_executor(job):
    return execute_recorded(job.cmd, job.args, job.client)


def h_history(args):
    """Gecmis satirlari (en yeni sonda) + undo durumu."""
    if _hist["store"] is None:
        _history_setup(_state.get("cfg") or current_config())
    store, tracker = _hist["store"], _hist["tracker"]
    _reconcile_pending()
    limit = int(args.get("limit") or 50)
    tool = (args.get("tool") or "").strip()
    entries = store.load(max(limit * 4, 200))
    undone = mhist.undone_ids(entries)
    try:
        stack = _undo_names()
    except Exception:  # noqa: BLE001
        stack = []
    pending = (_hist.get("pending") or {}).get("id")
    out = []
    for e in entries:
        if tool and e.get("tool") != tool:
            continue
        e = dict(e)
        if e.get("changing"):
            if e["id"] in undone:
                e["undo_state"] = "undone"
            elif tracker.position(e["id"], stack) is not None:
                e["undo_state"] = "on_stack"
            elif e["id"] == pending:
                e["undo_state"] = "pending"
            else:
                e["undo_state"] = "gone"
        if not bool(args.get("full", False)):
            e.pop("args", None)
            for k in ("output", "error"):
                if k in e and len(e[k]) > 300:
                    e[k] = e[k][:300] + "..."
        out.append(e)
    return {"count": len(out[-limit:]), "entries": out[-limit:],
            "undo_stack_top": stack[:10], "history_file": str(store.path)}


def h_undo_info(_args):
    _p, rt = _rt()
    _reconcile_pending()
    return {"current_levels": int(rt.theHold.getCurrentUndoLevels()),
            "max_levels": int(rt.theHold.getMaxUndoLevels()),
            "undo_names": _undo_names()[:50],
            "redo_names": [str(n) for n in rt.theHold.getRedoNames()][:20],
            "ai_undo_enabled": bool((_state.get("cfg") or current_config()).get("ai_undo_enabled", True))}


def h_undo(args):
    """max_undo araci: yalniz MCP'nin kendi kayitlari; arada kullanici adimi
    varsa reddeder (max_mcp_history.UndoTracker.plan_ai_undo)."""
    cfg = _state.get("cfg") or current_config()
    if not cfg.get("ai_undo_enabled", True):
        raise PermissionError("AI undo is disabled in the max-mcp settings window")
    if _hist["tracker"] is None:
        _history_setup(cfg)
    tracker = _hist["tracker"]
    to_id = args.get("to_history_id")
    steps = args.get("steps")
    try:
        undone = tracker.ai_undo(steps=None if to_id else int(steps or 1),
                                 to_id=int(to_id) if to_id else None)
    except ValueError as exc:
        raise PermissionError(str(exc))
    try:
        _rt()[1].redrawViews()
    except Exception:  # noqa: BLE001
        pass
    return {"undone_ids": undone, "undone_steps": len(undone), "undo_stack_top": _undo_names()[:5]}


HANDLERS.update({"history": h_history, "undo": h_undo, "undo_info": h_undo_info})


# --- yalniz ayar penceresi (ayni surec); TCP/MCP uzerinden erisilemez -----

def history_status(entry_id):
    if _hist["tracker"] is None:
        _history_setup(_state.get("cfg") or current_config())
    _reconcile_pending()
    return _hist["tracker"].status(int(entry_id))


def revert_to(entry_id, client="settings-window"):
    """Pencere: #entry_id kaydina KADAR (o dahil) geri al; arada kullanici
    adimlari varsa onlar da geri alinir -- onayi pencere alir."""
    if _hist["tracker"] is None:
        _history_setup(_state.get("cfg") or current_config())
    tracker, store = _hist["tracker"], _hist["store"]
    _reconcile_pending()
    st = tracker.status(int(entry_id))
    if not st["revertable"]:
        raise RuntimeError(st["reason"])
    own_before = tracker.own_positions()
    steps = tracker.revert(int(entry_id))
    undone = [own_before[i] for i in sorted(own_before) if i < steps]
    t = time.time()
    e = mhist.make_entry(store.next_id(), "revert", {"to_history_id": int(entry_id)}, client, t, time.time(),
                         True, result={"undone_steps": steps, "foreign_steps": st.get("foreign_steps", [])})
    e["undone_ids"] = undone
    store.append(e)
    try:
        _rt()[1].redrawViews()
    except Exception:  # noqa: BLE001
        pass
    return {"undone_steps": steps, "undone_ids": undone}


def restore_checkpoint(path, client="settings-window"):
    """Pencere: kontrol noktasini hold/fetch yoluyla yukler, boylece sahnenin
    dosya adi ve yolu degismez (probe: fetch-from-copied-cp). Undo yigini
    bosalir; sonraki elle yapilan isler gider -- onayi pencere alir."""
    _p, rt = _rt()
    path = str(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    hold = os.path.join(str(rt.getDir(rt.Name("autoback"))), "maxhold.mx")
    import shutil
    shutil.copyfile(path, hold)
    ok = rt.fetchMaxFile(quiet=True)
    if not ok:
        raise RuntimeError("fetchMaxFile failed for %s" % path)
    rt.setSaveRequired(True)
    if _hist["store"] is not None:
        t = time.time()
        _hist["store"].append(mhist.make_entry(_hist["store"].next_id(), "restore_checkpoint", {"path": path},
                                               client, t, time.time(), True, result={"restored": path}))
    return {"restored": path, "scene_file": _s(rt.maxFileName)}


def set_undo_levels(n):
    _p, rt = _rt()
    rt.theHold.setMaxUndoLevels(int(n))
    return int(rt.theHold.getMaxUndoLevels())


def rerun(entry_id, client="settings-window"):
    """Pencere: gecmisteki bir komutu ayni parametrelerle yeniden calistir."""
    if _hist["store"] is None:
        _history_setup(_state.get("cfg") or current_config())
    e = _hist["store"].get(int(entry_id))
    if e is None:
        raise KeyError("history #%s not found" % entry_id)
    if e["tool"] in ("undo", "revert", "restore_checkpoint", "history", "undo_info"):
        raise ValueError("cannot re-run a %s entry" % e["tool"])
    return execute_recorded(e["tool"], e.get("args") or {}, client)


# --------------------------------------------------------------------------
# Max icinde baslatma (ana thread pump'i)
# --------------------------------------------------------------------------

_state = {"listener": None, "timer": None, "pump_mode": None, "cfg": None,
          "last_error": None}


def current_config(overrides=None):
    """Ayar dosyasi + ortam degiskenleri (+ verilen override'lar)."""
    return mcfg.resolve(args=overrides)


def _make_listener(cfg):
    return Listener(HANDLERS, host=cfg["host"], port=int(cfg["port"]),
                    key=mcfg.effective_key(cfg), default_timeout=float(cfg["timeout"]))


def start(cfg=None, pump="qt", **overrides):
    """Max'in UI oturumunda cagir. pump='qt' -> PySide6 QTimer ana thread'de
    kuyrugu bosaltir. pump='none' -> yalnizca soket; pump() disaridan
    (ornegin MAXScript dotNet timer'i) cagrilir. cfg verilmezse ayar dosyasi
    + ortam degiskenleri okunur; overrides (port=..., host=...) onlari ezer."""
    if _state["listener"] is not None:
        log("zaten calisiyor")
        return _state["listener"]
    cfg = current_config(overrides) if cfg is None else cfg
    ls = _make_listener(cfg)
    ls.executor = _recorded_executor
    _history_setup(cfg)
    try:
        ls.start()
    except OSError as exc:
        _state["last_error"] = "%s:%s baglanamadi: %s" % (cfg["host"], cfg["port"], exc)
        log(_state["last_error"])
        raise
    _state["listener"] = ls
    _state["cfg"] = cfg
    _state["pump_mode"] = pump
    _state["last_error"] = None
    if pump == "qt":
        from PySide6 import QtCore  # Max 2025 ile gelir
        if QtCore.QCoreApplication.instance() is None:
            ls.stop()
            _state["listener"] = None
            raise RuntimeError("QCoreApplication yok; pump='none' ile dotNet timer kullan")
        t = QtCore.QTimer()
        t.setInterval(int(cfg["pump_interval_ms"]))
        t.timeout.connect(lambda: ls.pump())
        t.start()
        _state["timer"] = t
        log("QTimer pump aktif (%d ms)" % int(cfg["pump_interval_ms"]))
    return ls


def restart(cfg=None, **overrides):
    pump = _state.get("pump_mode") or "qt"
    stop()
    return start(cfg, pump=pump, **overrides)


def is_running():
    ls = _state.get("listener")
    return bool(ls and ls.running)


def status():
    """Ayar penceresi ve max_info icin durum sozlugu."""
    ls = _state.get("listener")
    cfg = _state.get("cfg") or {}
    return {
        "version": VERSION,
        "running": bool(ls and ls.running),
        "host": ls.host if ls else cfg.get("host"),
        "port": ls.port if ls else cfg.get("port"),
        "key_enabled": bool(ls.key) if ls else False,
        "pump_mode": _state.get("pump_mode"),
        "pump_on_main_thread": threading.current_thread() is threading.main_thread(),
        "python": sys.version.split()[0],
        "stats": dict(ls.stats) if ls else None,
        "queue": ls.queue.qsize() if ls else 0,
        "uptime_s": round(time.time() - ls.started_at, 1) if ls and ls.started_at else None,
        "last_error": _state.get("last_error"),
        "config_path": str(mcfg.config_path()),
    }


def pump(max_jobs=1):
    """Dis timer'lar (MAXScript dotNet Forms.Timer, batch dongusu) icin.
    python.Execute her cagriyi tek bir undo seviyesine sardigi icin cagri
    basina TEK is islenir; boylece her degisiklik ayri seviye olur."""
    try:
        _reconcile_pending()  # onceki python.Execute'un seviyesi artik gorunur
    except Exception:  # noqa: BLE001
        pass
    ls = _state["listener"]
    return ls.pump(max_jobs=max_jobs) if ls else 0


def stop():
    t = _state.get("timer")
    if t is not None:
        try:
            t.stop()
        except Exception:  # noqa: BLE001
            pass
        _state["timer"] = None
    ls = _state.get("listener")
    if ls is not None:
        ls.stop()
        _state["listener"] = None
    _state["pump_mode"] = None


def serve_blocking(seconds=None, port=None, **overrides):
    """3dsmaxbatch testi: dinleyiciyi baslat, ana thread'i burada tut."""
    if port is not None:
        overrides["port"] = port
    cfg = current_config(overrides)
    ls = _make_listener(cfg)
    ls.executor = _recorded_executor
    _history_setup(cfg)
    ls.start()
    _state["listener"] = ls
    _state["cfg"] = cfg
    _state["pump_mode"] = "blocking"
    try:
        return ls.serve_blocking(seconds)
    finally:
        stop()
