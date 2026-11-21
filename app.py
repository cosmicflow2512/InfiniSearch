#!/usr/bin/env python3
"""InfiniSearch 2 - freie Indexer-Suche fuer InfiniDysk 1.x.

Suchen, Ein-Klick-Abspielen, Weiter/Zurueck, Favoriten (NZB lokal gespeichert),
Suchverlauf und geladene NZBs loeschen.

InfiniDysk-Endpunkte (Quellcode v1.7.1):
  GET  /api/search-indexers?q=&limit=                 x-api-key
  GET  /api/download-nzb?nzbBlobId=                   x-api-key
  POST /api/list-webdav-directory  (form directory)   x-api-key
  GET  /api?mode=addurl|queue|history                 apikey
  POST /api?mode=addfile (multipart nzbFile)          apikey
  GET  /api?mode=history&name=delete&value=&del_completed_files=1
  GET  /api?mode=queue&name=delete&value=
  GET  /view/<pfad>?downloadKey=HMAC-SHA256(FRONTEND_BACKEND_API_KEY, pfad)
"""
import base64, hashlib, hmac, json, os, re, threading, time, uuid
import urllib.parse, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.environ.get("INFINIDYSK_URL", "http://192.168.0.20:3008").rstrip("/")
PUBLIC = os.environ.get("INFINIDYSK_PUBLIC_URL", "").rstrip("/") or BASE
API_KEY = os.environ.get("API_KEY", "")
CATEGORY = os.environ.get("CATEGORY", "movies")
LIMIT = int(os.environ.get("SEARCH_LIMIT", "100"))
PORT = int(os.environ.get("PORT", "8090"))
APP_USER = os.environ.get("APP_USER", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DATA = os.environ.get("DATA_DIR", "/data")
TIMEOUT = 60
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg", ".flv"}

_lock = threading.RLock()
STATE_FILE = os.path.join(DATA, "state.json")
NZB_DIR = os.path.join(DATA, "nzbs")


class UpstreamError(Exception):
    pass


# ---------------------------------------------------------------- Zustand
def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("searches", [])   # [{q, ts}]
    s.setdefault("played", {})     # key -> {nzo, title, ts}
    s.setdefault("favorites", [])  # [{id, title, size, indexer, ts}]
    return s


STATE = None


def save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- InfiniDysk
def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise UpstreamError(f"InfiniDysk HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    except urllib.error.URLError as e:
        raise UpstreamError(f"InfiniDysk nicht erreichbar ({BASE}): {e.reason}")


def _req(method, path, params=None, form=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("x-api-key", API_KEY)
    if data is not None:
        r.add_header("Content-Type", "application/x-www-form-urlencoded")
    return json.loads(_open(r).decode("utf-8") or "{}")


def sab(mode, **params):
    params.update(mode=mode, output="json", apikey=API_KEY)
    url = BASE + "/api?" + urllib.parse.urlencode(params)
    return json.loads(_open(urllib.request.Request(url)).decode("utf-8") or "{}")


def sab_addfile(nzb_bytes, name):
    boundary = uuid.uuid4().hex
    fname = name + ".nzb"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"nzbFile\"; filename=\"{fname}\"\r\n"
            f"Content-Type: application/x-nzb\r\n\r\n").encode() + nzb_bytes + f"\r\n--{boundary}--\r\n".encode()
    params = urllib.parse.urlencode(dict(mode="addfile", output="json", apikey=API_KEY, nzbname=name, cat=CATEGORY))
    r = urllib.request.Request(BASE + "/api?" + params, data=body, method="POST")
    r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    res = json.loads(_open(r).decode("utf-8") or "{}")
    ids = res.get("nzo_ids") or []
    if not ids:
        raise UpstreamError("addfile lieferte keine nzo_id: " + json.dumps(res)[:300])
    return ids[0]


def clean_name(title):
    return re.sub(r"[^\w.\- ()\[\]]+", "_", title or "").strip()[:200] or "release"


def add_url(nzb_url, title):
    with _lock:
        p = STATE["played"].get(nzb_url)
        if p:
            return p["nzo"]
    res = sab("addurl", name=nzb_url, nzbname=clean_name(title), cat=CATEGORY)
    ids = res.get("nzo_ids") or []
    if not ids:
        raise UpstreamError("addurl lieferte keine nzo_id: " + json.dumps(res)[:300])
    with _lock:
        STATE["played"][nzb_url] = {"nzo": ids[0], "title": title, "ts": int(time.time())}
        save()
    return ids[0]


def add_fav(fav_id):
    key = "fav:" + fav_id
    with _lock:
        p = STATE["played"].get(key)
        fav = next((f for f in STATE["favorites"] if f["id"] == fav_id), None)
    if p:
        return p["nzo"]
    if not fav:
        raise UpstreamError("Favorit nicht gefunden")
    with open(os.path.join(NZB_DIR, fav_id + ".nzb"), "rb") as f:
        nzo = sab_addfile(f.read(), clean_name(fav["title"]))
    with _lock:
        STATE["played"][key] = {"nzo": nzo, "title": fav["title"], "ts": int(time.time())}
        save()
    return nzo


def history_slot(nzo):
    hist = sab("history", nzo_ids=nzo, limit=50).get("history", {}).get("slots", [])
    return next((s for s in hist if s.get("nzo_id") == nzo), None)


def list_dir(path):
    res = _req("POST", "/api/list-webdav-directory", form={"directory": path})
    return res.get("items") or res.get("Items") or []


def walk(path, depth=0):
    out = []
    for it in list_dir(path):
        name = it.get("name") or it.get("Name")
        child = path.rstrip("/") + "/" + name
        if it.get("isDirectory") or it.get("IsDirectory"):
            if depth < 4:
                out += walk(child, depth + 1)
        else:
            out.append({"path": child, "name": name, "size": it.get("size") or it.get("Size") or 0})
    return out


def view_url(path):
    item = path.lstrip("/")
    key = hmac.new(API_KEY.encode(), item.encode(), hashlib.sha256).hexdigest()
    quoted = "/".join(urllib.parse.quote(p, safe="") for p in item.split("/"))
    return f"{PUBLIC}/view/{quoted}?downloadKey={key}"


def status(nzo):
    slot = history_slot(nzo)
    if slot:
        if (slot.get("status") or "").lower() == "completed":
            files = walk(f"/content/{slot.get('category') or CATEGORY}/{slot.get('name')}")
            for f in files:
                f["video"] = os.path.splitext(f["name"])[1].lower() in VIDEO_EXT
                f["url"] = view_url(f["path"])
            files.sort(key=lambda f: (not f["video"], -int(f["size"] or 0)))
            return {"state": "ready", "name": slot.get("name"), "files": files}
        return {"state": "failed", "error": slot.get("fail_message") or slot.get("status")}
    q = sab("queue").get("queue", {}).get("slots", [])
    qs = next((s for s in q if s.get("nzo_id") == nzo), None)
    if qs:
        return {"state": "queued", "status": qs.get("status"), "percentage": qs.get("percentage")}
    return {"state": "pending"}


def make_favorite(body):
    """NZB aus InfiniDysk holen (nach dem Einreihen) und lokal speichern."""
    nzo = body.get("nzo") or add_url(body["nzbUrl"], body.get("title"))
    deadline = time.time() + 90
    slot = None
    while time.time() < deadline:
        slot = history_slot(nzo)
        if slot:
            break
        time.sleep(1.5)
    if not slot or not slot.get("nzb_blob_id"):
        raise UpstreamError("NZB noch nicht verfuegbar - bitte erneut versuchen")
    url = BASE + "/api/download-nzb?" + urllib.parse.urlencode({"nzbBlobId": slot["nzb_blob_id"]})
    r = urllib.request.Request(url)
    r.add_header("x-api-key", API_KEY)
    data = _open(r)
    fav_id = uuid.uuid4().hex[:16]
    with open(os.path.join(NZB_DIR, fav_id + ".nzb"), "wb") as f:
        f.write(data)
    fav = {"id": fav_id, "title": body.get("title") or slot.get("name"), "size": body.get("size") or slot.get("bytes"),
           "indexer": body.get("indexer") or "", "nzbUrl": body.get("nzbUrl") or "", "ts": int(time.time())}
    with _lock:
        STATE["favorites"].insert(0, fav)
        STATE["played"]["fav:" + fav_id] = {"nzo": nzo, "title": fav["title"], "ts": int(time.time())}
        save()
    return fav


def delete_fav(fav_id):
    with _lock:
        STATE["favorites"] = [f for f in STATE["favorites"] if f["id"] != fav_id]
        save()
    try:
        os.remove(os.path.join(NZB_DIR, fav_id + ".nzb"))
    except FileNotFoundError:
        pass


def cleanup_loaded():
    """Alle von InfiniSearch eingereihten NZBs aus InfiniDysk entfernen (inkl. Mount)."""
    with _lock:
        ids = sorted({p["nzo"] for p in STATE["played"].values()})
    errors = []
    for i in range(0, len(ids), 50):
        chunk = ",".join(ids[i:i + 50])
        for mode, extra in (("queue", {}), ("history", {"del_completed_files": "1"})):
            try:
                sab(mode, name="delete", value=chunk, **extra)
            except UpstreamError as e:
                errors.append(str(e))
    if not errors:
        with _lock:
            STATE["played"] = {}
            save()
    return {"removed": len(ids), "errors": errors}


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "InfiniSearch/2.1"

    def log_message(self, fmt, *a):
        # keine Suchbegriffe/Pfade ins Log schreiben (Privatsphaere)
        print("%s - %s %s" % (self.address_string(), self.command, urllib.parse.urlparse(self.path).path), flush=True)

    def _auth(self):
        if not APP_PASSWORD:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(hdr[6:]).decode().partition(":")
                if hmac.compare_digest(u, APP_USER) and hmac.compare_digest(p, APP_PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="InfiniSearch"')
        self.end_headers()
        return False

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _dispatch(self, method):
        if self.path == "/healthz":
            return self._json({"ok": True})
        if not self._auth():
            return
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        route = (method, u.path)
        try:
            if route == ("GET", "/"):
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif route == ("GET", "/api/search"):
                q = qs.get("q", "").strip()
                if not q:
                    return self._json({"error": "Suchbegriff fehlt"}, 400)
                with _lock:
                    STATE["searches"] = [s for s in STATE["searches"] if s["q"] != q]
                    STATE["searches"].insert(0, {"q": q, "ts": int(time.time())})
                    del STATE["searches"][200:]
                    save()
                self._json(_req("GET", "/api/search-indexers", params={"q": q, "limit": LIMIT}))
            elif route == ("GET", "/api/searches"):
                with _lock:
                    self._json(STATE["searches"])
            elif route == ("DELETE", "/api/searches"):
                with _lock:
                    STATE["searches"] = []
                    save()
                self._json({"ok": True})
            elif route == ("POST", "/api/play"):
                b = self._body()
                if b.get("favId"):
                    return self._json({"nzo": add_fav(b["favId"])})
                if not b.get("nzbUrl"):
                    return self._json({"error": "nzbUrl fehlt"}, 400)
                self._json({"nzo": add_url(b["nzbUrl"], b.get("title"))})
            elif route == ("GET", "/api/status"):
                self._json(status(qs.get("nzo", "")))
            elif route == ("GET", "/api/favorites"):
                with _lock:
                    self._json(STATE["favorites"])
            elif route == ("POST", "/api/favorites"):
                self._json(make_favorite(self._body()))
            elif route == ("DELETE", "/api/favorites"):
                delete_fav(qs.get("id", ""))
                self._json({"ok": True})
            elif route == ("GET", "/api/loaded"):
                with _lock:
                    self._json({"count": len({p["nzo"] for p in STATE["played"].values()})})
            elif route == ("DELETE", "/api/loaded"):
                self._json(cleanup_loaded())
            else:
                self._json({"error": "not found"}, 404)
        except UpstreamError as e:
            self._json({"error": str(e)}, 502)
        except (OSError, ValueError, KeyError) as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


PAGE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>InfiniSearch</title>
<style>
:root{color-scheme:dark;--bg:#0b0b0d;--card:#16161a;--line:#2a2a31;--acc:#6d5dfc;--mut:#9a9aa5;--fav:#f5c542}
*{box-sizing:border-box}body{margin:0;font:15px system-ui,sans-serif;background:var(--bg);color:#eee}
header{position:sticky;top:0;background:var(--bg);padding:12px 14px;border-bottom:1px solid var(--line);z-index:3}
.wrap{max-width:1100px;margin:auto}
form{display:flex;gap:8px}
input{flex:1;padding:11px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:#eee;font-size:16px}
button{padding:9px 13px;border:0;border-radius:8px;background:var(--acc);color:#fff;font-weight:600;cursor:pointer;white-space:nowrap}
button.sec{background:#2a2a31}button.danger{background:#7a2330}button:disabled{opacity:.45;cursor:default}
nav{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}nav button{background:transparent;border:1px solid var(--line)}
nav button.on{background:#2a2a31}
select{padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:#eee}
main{max-width:1100px;margin:auto;padding:14px}
#player{display:none;background:#000;border-radius:10px;overflow:hidden;margin-bottom:14px}
#player video{width:100%;max-height:68vh;display:block;background:#000}
.bar{padding:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--card)}
#pt{flex:1 1 100%;word-break:break-all;color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;align-items:center;padding:10px;border-bottom:1px solid var(--line)}
.row.cur{background:#1d1a33}.row .t{flex:1;word-break:break-all}
.meta{color:var(--mut);font-size:12px;margin-top:3px}
#msg{color:var(--mut);padding:6px 0}.err{color:#ff7b7b}
.files{padding:2px 10px 8px 30px;font-size:13px}.files a{color:#b9b0ff;cursor:pointer;display:block;padding:3px 0}
.star{background:transparent;border:1px solid var(--line);color:var(--fav);font-size:16px;padding:7px 10px}
.hist a{display:inline-block;margin:4px;padding:6px 10px;border-radius:20px;background:var(--card);color:#ddd;cursor:pointer;text-decoration:none}
.box{background:var(--card);border-radius:10px;padding:14px;margin-bottom:12px}
.box p{color:var(--mut);margin:6px 0 12px}
kbd{background:#2a2a31;border-radius:4px;padding:1px 5px;font-size:12px}
</style></head><body>
<header><div class="wrap">
<form id="f"><input id="q" placeholder="Indexer durchsuchen, z.B. Blume" autocomplete="off" list="recent"><datalist id="recent"></datalist><button>Suchen</button></form>
<nav><select id="flt" title="Filter"><option value="video">🎬 Nur Videos</option><option value="nobook">Ohne Bücher/Audio/Software</option><option value="all">Alles</option></select><button data-tab="res" class="on">Ergebnisse</button><button data-tab="fav">★ Favoriten</button><button data-tab="priv">Verlauf &amp; Privatsphäre</button></nav>
</div></header>
<main>
<div id="player"><video id="v" controls playsinline></video>
<div class="bar"><span id="pt"></span>
<button class="sec" id="prev">⏮ Zurück</button><button class="sec" id="back">−10 s</button><button class="sec" id="fwd">+30 s</button><button id="next">Weiter ⏭</button>
<button class="star" id="pfav" title="Favorit">☆</button>
<label style="color:var(--mut);font-size:13px"><input type="checkbox" id="auto" style="flex:none"> Autoplay weiter</label>
<button class="sec" id="copy">Link kopieren</button><a id="vlc"><button class="sec">In VLC</button></a></div></div>
<div id="msg"></div>
<section id="tab-res"><div id="list"></div></section>
<section id="tab-fav" hidden><div id="favs"></div></section>
<section id="tab-priv" hidden>
 <div class="box"><b>Suchverlauf</b><p>Letzte Suchen (nur in InfiniSearch gespeichert).</p><div class="hist" id="hist"></div><br>
  <button class="danger" id="delhist">Suchverlauf löschen</button></div>
 <div class="box"><b>Geladene NZBs</b><p>Alle NZBs, die InfiniSearch in InfiniDysk eingereiht hat, inkl. Mount entfernen. Favoriten bleiben erhalten (NZB liegt lokal) und lassen sich jederzeit neu abspielen. Einträge aus Sonarr/Radarr/Stremio werden nicht angefasst.</p>
  <span id="loaded" style="color:var(--mut)"></span><br><br><button class="danger" id="delloaded">Geladene NZBs löschen</button></div>
 <div class="box"><button class="danger" id="delall">Alles löschen (Verlauf + geladene NZBs)</button><p>Favoriten bleiben.</p></div>
 <div class="box"><b>Tastatur</b><p><kbd>N</kbd> weiter · <kbd>P</kbd> zurück · <kbd>F</kbd> Favorit · <kbd>←</kbd>/<kbd>→</kbd> spulen · <kbd>Leertaste</kbd> Pause</p></div>
</section>
</main>
<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const fmt=b=>{b=+b;if(!b)return'';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i>2?2:0)+' '+u[i]};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const api=async(p,o)=>{const r=await fetch(p,o);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d};
const NONVIDEO=/(^|[^a-z0-9])(epub|mobi|azw3?|pdf|cbr|cbz|djvu|e-?books?|magazines?|zeitschrift|audiobooks?|h(ö|oe)rb(u|ü)ch(er)?|mp3|flac|m4b|aac|ogg|wav|alac|320kbps|24bit|discography|exe|apk|dmg|msi)([^a-z0-9]|$)/i;
const VIDEO=/(^|[^a-z0-9])(2160p|1080[pi]|720p|576p|540p|480p|4k|uhd|x\.?26[45]|h\.?26[45]|hevc|avc|xvid|divx|av1|vp9|web-?dl|web-?rip|web|bluray|blu-ray|bdrip|brrip|bd|remux|dvd-?rip|dvd|dvdr|hdtv|hdrip|tvrip|pdtv|sdtv|mkv|mp4|avi|m2ts|wmv|s\d{1,2}e\d{1,3}|s\d{1,2}|e\d{2,4}|episode|folge|staffel|season)([^a-z0-9]|$)/i;
const keep=(t,m)=>m==='all'?true:m==='nobook'?!NONVIDEO.test(t):(VIDEO.test(t)&&!NONVIDEO.test(t));
let results=[],shown=[],favs=[],list=[],listKind='res',cur=-1,curUrl='',curNzo=null,run=0;
const msg=(h,e)=>$('#msg').innerHTML=e?'<span class="err">'+esc(h)+'</span>':h;

function tab(t){$$('nav button').forEach(b=>b.classList.toggle('on',b.dataset.tab===t));['res','fav','priv'].forEach(x=>$('#tab-'+x).hidden=x!==t);
 if(t==='fav')loadFavs();if(t==='priv')loadPriv()}
$$('nav button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));

async function loadRecent(){try{const s=await api('api/searches');$('#recent').innerHTML=s.slice(0,20).map(x=>`<option value="${esc(x.q)}">`).join('');return s}catch(e){return[]}}
$('#f').onsubmit=async e=>{e.preventDefault();const q=$('#q').value.trim();if(!q)return;tab('res');
 msg('Suche läuft …');$('#list').innerHTML='';
 try{const d=await api('api/search?q='+encodeURIComponent(q));results=d.results||[];
  const bad=(d.indexers||[]).filter(i=>!i.ok).map(i=>i.name+': '+i.error);
  msg(results.length+' Treffer gesamt'+(bad.length?' <span class="err">· '+esc(bad.join(' | '))+'</span>':''));
  renderRes();loadRecent()}catch(err){msg(err.message,1)}};

const isFav=x=>favs.some(f=>f.nzbUrl&&f.nzbUrl===x.nzbUrl);
function rowHtml(x,i,kind){const fav=kind==='fav'||isFav(x);return `<div><div class="row" id="${kind}${i}"><div class="t">${esc(x.title)}
 <div class="meta">${fmt(x.size)} · ${esc(x.indexer||'')} · ${x.posted?new Date(x.posted).toLocaleDateString('de-DE'):x.ts?'gespeichert '+new Date(x.ts*1000).toLocaleDateString('de-DE'):''}</div></div>
 <button data-play="${i}" data-kind="${kind}">▶ Play</button>
 ${kind==='fav'?`<button class="danger" data-unfav="${i}">Entfernen</button>`:`<button class="star" data-fav="${i}" title="Favorit">${fav?'★':'☆'}</button>`}</div>
 <div class="files" id="fi-${kind}${i}"></div></div>`}
function renderRes(){const m=$('#flt').value;shown=results.filter(x=>keep(x.title||'',m));
 const hid=results.length-shown.length;$('#list').innerHTML=(hid?`<div class="meta" style="padding:6px 10px">${hid} von ${results.length} Treffern ausgeblendet (Filter: ${$('#flt').selectedOptions[0].text})</div>`:'')+shown.map((x,i)=>rowHtml(x,i,'res')).join('');bind()}
$('#flt').value=localStorage.getItem('flt')||'video';$('#flt').onchange=()=>{localStorage.setItem('flt',$('#flt').value);renderRes()};
function renderFavs(){$('#favs').innerHTML=favs.length?favs.map((x,i)=>rowHtml(x,i,'fav')).join(''):'<p style="color:var(--mut)">Noch keine Favoriten.</p>';bind()}
function bind(){$$('[data-play]').forEach(b=>b.onclick=()=>{listKind=b.dataset.kind;list=listKind==='fav'?favs:shown;play(+b.dataset.play)});
 $$('[data-fav]').forEach(b=>b.onclick=()=>favorite(shown[+b.dataset.fav],null,b));
 $$('[data-unfav]').forEach(b=>b.onclick=async()=>{if(!confirm('Favorit entfernen?'))return;await api('api/favorites?id='+favs[+b.dataset.unfav].id,{method:'DELETE'});loadFavs()})}
async function loadFavs(){try{favs=await api('api/favorites');renderFavs()}catch(e){msg(e.message,1)}}

async function play(i){if(i<0||i>=list.length)return;const my=++run;cur=i;const x=list[i];
 $$('.row').forEach(r=>r.classList.remove('cur'));const row=$('#'+listKind+i);row&&row.classList.add('cur');
 const box=$('#fi-'+listKind+i);const say=t=>{if(box)box.textContent=t;$('#pt').textContent=(i+1)+'/'+list.length+' · '+x.title+' — '+t};
 $('#player').style.display='block';const v=$('#v');v.pause();v.removeAttribute('src');v.load();updNav();say('Wird eingereiht …');
 try{const d0=await api('api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(listKind==='fav'?{favId:x.id}:{nzbUrl:x.nzbUrl,title:x.title})});
  curNzo=d0.nzo;
  for(let n=0;n<400;n++){if(my!==run)return;const d=await api('api/status?nzo='+encodeURIComponent(curNzo));if(my!==run)return;
   if(d.state==='ready'){const fs=d.files||[];
    if(box){box.innerHTML=fs.map((f,j)=>`<a data-j="${j}">${f.video?'🎬':'📄'} ${esc(f.name)} (${fmt(f.size)})</a>`).join('');box.querySelectorAll('a').forEach(a=>a.onclick=()=>show(fs[a.dataset.j],x,i))}
    const vf=fs.find(f=>f.video);if(!vf){say('Kein Video in diesem Release');if($('#auto').checked)play(i+1);return}
    show(vf,x,i);return}
   if(d.state==='failed')throw new Error('Fehlgeschlagen: '+(d.error||''));
   say(d.state==='queued'?`In der Queue: ${d.status||''} ${d.percentage??''}%`:'Warte auf InfiniDysk …');
   await new Promise(s=>setTimeout(s,1500))}
  say('Zeitüberschreitung')}catch(err){if(my===run){say('Fehler: '+err.message);if(box)box.innerHTML='<span class="err">'+esc(err.message)+'</span>'}}}
function show(f,x,i){curUrl=f.url;$('#pt').textContent=(i+1)+'/'+list.length+' · '+f.name;const v=$('#v');v.src=f.url;v.play().catch(()=>{});
 $('#vlc').href='vlc://'+f.url;updNav();window.scrollTo({top:0,behavior:'smooth'})}
function updNav(){$('#prev').disabled=cur<=0;$('#next').disabled=cur>=list.length-1;const x=list[cur];
 $('#pfav').textContent=(listKind==='fav'||(x&&isFav(x)))?'★':'☆'}
$('#v').onerror=()=>{if($('#v').getAttribute('src'))$('#pt').textContent+=' — Browser kann das Format evtl. nicht abspielen: Link kopieren / In VLC.'};
$('#v').onended=()=>{if($('#auto').checked)play(cur+1)};
$('#next').onclick=()=>play(cur+1);$('#prev').onclick=()=>play(cur-1);
$('#back').onclick=()=>{$('#v').currentTime-=10};$('#fwd').onclick=()=>{$('#v').currentTime+=30};
$('#pfav').onclick=()=>{if(listKind==='fav'||cur<0)return;favorite(list[cur],curNzo,null)};
async function favorite(x,nzo,btn){if(isFav(x))return;const el=btn||$('#pfav');el.disabled=true;el.textContent='…';
 try{await api('api/favorites',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nzo:nzo||undefined,nzbUrl:x.nzbUrl,title:x.title,size:x.size,indexer:x.indexer})});
  await loadFavs();renderRes();updNav()}catch(e){alert('Favorit fehlgeschlagen: '+e.message);el.textContent='☆'}finally{el.disabled=false}}
$('#copy').onclick=()=>{navigator.clipboard?.writeText(curUrl).then(()=>{$('#copy').textContent='Kopiert ✓';setTimeout(()=>$('#copy').textContent='Link kopieren',1500)},()=>prompt('Link:',curUrl))};
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return;const v=$('#v');
 if(e.key==='n'||e.key==='N')play(cur+1);else if(e.key==='p'||e.key==='P')play(cur-1);else if(e.key==='f'||e.key==='F')$('#pfav').click();
 else if(e.key==='ArrowRight'&&document.activeElement!==v){v.currentTime+=10;e.preventDefault()}
 else if(e.key==='ArrowLeft'&&document.activeElement!==v){v.currentTime-=10;e.preventDefault()}
 else if(e.key===' '&&document.activeElement!==v&&v.getAttribute('src')){v.paused?v.play():v.pause();e.preventDefault()}});

async function loadPriv(){const s=await loadRecent();$('#hist').innerHTML=s.length?s.map(x=>`<a data-q="${esc(x.q)}">${esc(x.q)}</a>`).join(''):'<span style="color:var(--mut)">leer</span>';
 $$('#hist a').forEach(a=>a.onclick=()=>{$('#q').value=a.dataset.q;$('#f').requestSubmit()});
 try{const l=await api('api/loaded');$('#loaded').textContent=l.count+' NZB(s) von InfiniSearch in InfiniDysk geladen'}catch(e){$('#loaded').textContent=e.message}}
async function delHist(){await api('api/searches',{method:'DELETE'})}
async function delLoaded(){const v=$('#v');v.pause();v.removeAttribute('src');v.load();$('#player').style.display='none';run++;
 const r=await api('api/loaded',{method:'DELETE'});if(r.errors&&r.errors.length)throw new Error(r.errors.join(' | '));return r.removed}
$('#delhist').onclick=async()=>{if(!confirm('Suchverlauf löschen?'))return;await delHist();loadPriv()};
$('#delloaded').onclick=async()=>{if(!confirm('Alle geladenen NZBs aus InfiniDysk entfernen?'))return;try{const n=await delLoaded();alert(n+' entfernt')}catch(e){alert(e.message)}loadPriv()};
$('#delall').onclick=async()=>{if(!confirm('Suchverlauf UND geladene NZBs löschen? Favoriten bleiben.'))return;
 try{await delHist();await delLoaded();results=[];renderRes();$('#q').value='';msg('Gelöscht.')}catch(e){alert(e.message)}loadPriv()};
loadRecent();loadFavs();
</script></body></html>"""

if __name__ == "__main__":
    os.makedirs(NZB_DIR, exist_ok=True)
    STATE = _load()
    save()
    if not API_KEY:
        print("WARNUNG: API_KEY ist leer - InfiniDysk wird Anfragen ablehnen.", flush=True)
    print(f"InfiniSearch 2 auf :{PORT} -> {BASE} (oeffentlich {PUBLIC}), Kategorie {CATEGORY}, Daten {DATA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
