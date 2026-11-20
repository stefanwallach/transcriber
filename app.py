"""Transcriber Beta - turn audio or video into editable text with Deepgram.

Run:  python app.py            (desktop window)
      python app.py --browser  (opens http://127.0.0.1:8765 in the browser)
Keys:  %APPDATA%\\Transcriber\\key.txt (Deepgram), gemini_key.txt (Google Gemini).
Transcripts live in %APPDATA%\\Transcriber\\library\\<id>\\ (doc.json, meta.json, raw.json, media.*).
"""
import hashlib
import http.client
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

VERSION = "0.3.0-beta"
UPDATE_REPO = "stefanwallach/transcriber"
PORT = 8765
FROZEN = bool(getattr(sys, "frozen", False))
# Inside the packaged .exe, bundled files live in a folder PyInstaller unpacks to.
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Transcriber"
KEY_FILES = {"deepgram": CONFIG_DIR / "key.txt", "gemini": CONFIG_DIR / "gemini_key.txt"}
PREFS_FILE = CONFIG_DIR / "prefs.json"
LIBRARY = CONFIG_DIR / "library"
DG_HOST = "api.deepgram.com"
UA = f"Transcriber/{VERSION}"
GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]

JOBS = {}            # job id -> state dict (cancelled, conn, phase, pct, error, doc)
JOBS_LOCK = threading.Lock()
SAVE_LOCK = threading.Lock()
DIRTY = {}           # doc id -> time the page said it has unsaved edits
UPDATE = {"phase": "idle", "pct": 0, "error": None}
PROJECT = {"id": None}


# ================================================================ storage

def get_key(kind="deepgram"):
    if kind == "deepgram" and os.environ.get("DEEPGRAM_API_KEY", "").strip():
        return os.environ["DEEPGRAM_API_KEY"].strip()
    try:
        return KEY_FILES[kind].read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def prefs():
    return read_json(PREFS_FILE, {}) or {}


def safe_id(value):
    return value if re.fullmatch(r"[a-f0-9]{32}", value or "") else None


def doc_dir(doc_id):
    return LIBRARY / doc_id


def load_doc(doc_id):
    return read_json(doc_dir(doc_id) / "doc.json") if safe_id(doc_id) else None


def media_path(doc_id):
    for p in doc_dir(doc_id).glob("media.*"):
        return p
    return None


def plain_text(doc):
    return "\n".join(s["text"] for s in doc["segments"])


def meta_for(doc):
    text = plain_text(doc)
    return {
        "id": doc["id"], "title": doc["title"], "created": doc["created"],
        "duration": doc.get("duration"), "language": doc.get("language"),
        "words": len(text.split()), "hasMedia": bool(doc.get("media")),
        "folder": doc.get("folder") or "", "cost": doc.get("cost"),
        "site": (doc.get("sourceInfo") or {}).get("site"), "uploader": (doc.get("sourceInfo") or {}).get("uploader"),
        "preview": text[:160],
    }


def save_doc(doc):
    with SAVE_LOCK:
        doc["updated"] = time.time()
        write_json(doc_dir(doc["id"]) / "doc.json", doc)
        write_json(doc_dir(doc["id"]) / "meta.json", meta_for(doc))


def all_metas():
    if not LIBRARY.exists():
        return []
    out = [m for m in (read_json(d / "meta.json") for d in LIBRARY.iterdir()) if m]
    out.sort(key=lambda m: m["created"], reverse=True)
    return out


def search_library(query):
    q = query.strip().lower()
    items = []
    for meta in all_metas():
        snippet = None
        hit = q in meta["title"].lower()
        if not hit:
            doc = load_doc(meta["id"])
            text = plain_text(doc) if doc else ""
            i = text.lower().find(q)
            if i >= 0:
                hit = True
                start = max(0, i - 50)
                snippet = ("…" if start else "") + text[start:i + len(q) + 80]
        if hit:
            items.append(dict(meta, snippet=snippet))
    return items


# ================================================================ Deepgram response -> document

def word_of(w):
    return {"w": w.get("punctuated_word") or w.get("word") or w.get("w", ""),
            "s": round(w.get("start", w.get("s", 0)), 3), "e": round(w.get("end", w.get("e", 0)), 3),
            "c": round(w.get("confidence", w.get("c", 1)), 3)}


def chunk_words(words, diarize):
    """Paragraphs from bare words: new paragraph on speaker change, a long pause,
    a sentence end after ~45 words, or ~90 words."""
    segs, cur = [], None
    for raw in words:
        w = word_of(raw)
        sp = raw.get("speaker", raw.get("sp")) if diarize else None
        if (cur is None or sp != cur["speaker"] or w["s"] - cur["end"] > 1.5
                or len(cur["words"]) >= 90
                or (len(cur["words"]) >= 45 and re.search(r"[.?!]$", cur["words"][-1]["w"]))):
            cur = {"start": w["s"], "end": w["e"], "speaker": sp, "words": []}
            segs.append(cur)
        cur["words"].append(w)
        cur["end"] = w["e"]
    return segs


def build_segments(alt, diarize):
    words = alt.get("words") or []
    paras = ((alt.get("paragraphs") or {}).get("paragraphs")) or []
    segs = []
    if paras and words:
        # Paragraphs over ~80 words are broken at sentence ends so they stay readable.
        groups = []
        for p in paras:
            sents, count = [], 0
            for s in p.get("sentences") or [{"text": "", "end": p["end"]}]:
                sents.append(s)
                count += len(s["text"].split())
                if count >= 80:
                    groups.append((p, sents[-1]["end"]))
                    sents, count = [], 0
            if sents:
                groups.append((p, p["end"]))
        wi = 0
        for p, end in groups:
            pw = []
            while wi < len(words) and words[wi]["start"] <= end + 0.001:
                pw.append(word_of(words[wi]))
                wi += 1
            if pw:
                segs.append({"start": pw[0]["s"], "end": pw[-1]["e"],
                             "speaker": p.get("speaker") if diarize else None, "words": pw})
        if wi < len(words) and segs:
            segs[-1]["words"] += [word_of(w) for w in words[wi:]]
            segs[-1]["end"] = segs[-1]["words"][-1]["e"]
    elif words:
        segs = chunk_words(words, diarize)
    elif alt.get("transcript"):
        segs.append({"start": 0, "end": 0, "speaker": None, "words": None, "text": alt["transcript"]})
    return finish_segments(segs)


def finish_segments(segs):
    for s in segs:
        s["id"] = uuid.uuid4().hex[:10]
        if s.get("words"):
            s["text"] = " ".join(w["w"] for w in s["words"])
        s["edited"] = False
    return segs


def speakers_of(segs):
    speakers = {}
    for s in segs:
        if s["speaker"] is not None:
            speakers.setdefault(str(s["speaker"]), f"Speaker {s['speaker'] + 1}")
    return dict(sorted(speakers.items(), key=lambda kv: int(kv[0])))


def new_doc(doc_id, name, opts, media_name, segs, duration, language, summary="", request_id=None):
    return {
        "id": doc_id, "version": 2,
        "title": re.sub(r"\.[^.]+$", "", name).strip() or "Untitled",
        "source": name, "created": time.time(), "duration": duration,
        "language": language, "model": opts.get("model"), "options": opts, "media": media_name,
        "speakers": speakers_of(segs), "summary": summary, "segments": segs,
        "folder": opts.get("folder") or "", "bookmarks": [], "writeups": [],
        "requestId": request_id, "cost": None,
    }


def doc_from_deepgram(doc_id, name, dg, opts, media_name):
    ch = dg["results"]["channels"][0]
    alt = ch["alternatives"][0]
    segs = build_segments(alt, opts.get("diarize"))
    summary = ((dg["results"].get("summary") or {}).get("short")) or ""
    summary = re.sub(r"\[Speaker \d+:?\]\s*", "", summary).strip()
    return new_doc(doc_id, name, opts, media_name, segs, dg.get("metadata", {}).get("duration"),
                   ch.get("detected_language") or opts.get("language"), summary,
                   dg.get("metadata", {}).get("request_id"))


# ================================================================ Deepgram calls

class Cancelled(Exception):
    pass


class DGError(Exception):
    pass


class TeeReader:
    """Reads exactly `remaining` bytes, copying them to `out` and reporting progress."""

    def __init__(self, f, remaining, out, state, eof_is_cancel=True):
        self.f, self.total, self.remaining, self.out, self.state = f, remaining, remaining, out, state
        self.eof_is_cancel = eof_is_cancel

    def read(self, n=-1):
        if self.state["cancelled"]:
            raise Cancelled()
        if self.remaining <= 0:
            return b""
        n = self.remaining if n is None or n < 0 else min(n, self.remaining)
        chunk = self.f.read(n)
        if not chunk:
            raise Cancelled()
        self.remaining -= len(chunk)
        if self.out:
            self.out.write(chunk)
        self.state["pct"] = 1 - self.remaining / max(self.total, 1)
        if self.remaining <= 0 and self.state.get("phase") == "uploading":
            self.state["phase"] = "transcribing"
            self.state["t0"] = time.time()
        return chunk


def dg_params(opts):
    params = [("model", opts.get("model") or "nova-3"), ("smart_format", "true"), ("punctuate", "true"),
              ("paragraphs", "true"), ("utterances", "true")]
    if opts.get("diarize"):
        params.append(("diarize", "true"))
    if opts.get("language") == "auto":
        params.append(("detect_language", "true"))
    else:
        params.append(("language", opts.get("language") or "en"))
    if opts.get("filler"):
        params.append(("filler_words", "true"))
    if opts.get("profanity"):
        params.append(("profanity_filter", "true"))
    if opts.get("redact"):
        params += [("redact", "pci"), ("redact", "ssn"), ("redact", "pii")]
    if opts.get("summary"):
        params.append(("summarize", "v2"))
    for t in opts.get("keyterms") or []:
        params.append(("keyterm", t) if (opts.get("model") or "nova-3").startswith("nova-3") else ("keywords", t))
    return params


def dg_error_text(status, raw):
    detail = raw.decode("utf-8", "replace")
    try:
        j = json.loads(detail)
        detail = j.get("err_msg") or j.get("message") or detail
    except ValueError:
        pass
    if status in (401, 403):
        return "Deepgram didn't accept the API key. Check it in Settings."
    if status == 402:
        return "Your Deepgram account is out of credit. Add credit at console.deepgram.com."
    return detail


def deepgram_listen(state, key, params, body, length, ctype):
    """POST to /v1/listen. `body` is a file-like reader or bytes. Returns parsed JSON."""
    conn = http.client.HTTPSConnection(DG_HOST, timeout=3600, blocksize=1 << 16)
    state["conn"] = conn
    try:
        conn.request("POST", "/v1/listen?" + urllib.parse.urlencode(params), body=body,
                     headers={"Authorization": "Token " + key, "Content-Type": ctype,
                              "Content-Length": str(length), "User-Agent": UA})
        if state.get("phase") == "uploading":
            state["phase"] = "transcribing"
            state["t0"] = time.time()
        resp = conn.getresponse()
        raw = resp.read()
    except Cancelled:
        raise
    except Exception as e:
        if state["cancelled"]:
            raise Cancelled()
        raise DGError("Couldn't reach Deepgram. Check your internet connection. (" + str(e) + ")")
    finally:
        conn.close()
        state["conn"] = None
    if state["cancelled"]:
        raise Cancelled()
    if resp.status != 200:
        raise DGError(dg_error_text(resp.status, raw))
    return json.loads(raw)


def dg_get(path, key):
    req = urllib.request.Request("https://" + DG_HOST + path,
                                 headers={"Authorization": "Token " + key, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def project_id(key):
    if not PROJECT["id"]:
        projects = dg_get("/v1/projects", key).get("projects") or []
        PROJECT["id"] = projects[0]["project_id"] if projects else None
    return PROJECT["id"]


def fetch_cost_later(doc_id, request_id):
    """Deepgram's usage record appears a little after the request; look it up in the background."""
    if not request_id:
        return

    def work():
        key = get_key()
        for wait in (4, 10, 30, 90):
            time.sleep(wait)
            try:
                pid = project_id(key)
                if not pid:
                    return
                rec = dg_get(f"/v1/projects/{pid}/requests/{request_id}", key)
                usd = ((rec.get("response") or {}).get("details") or {}).get("usd")
                if usd is not None:
                    doc = load_doc(doc_id)
                    if doc:
                        doc["cost"] = float(usd)
                        save_doc(doc)
                    return
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return  # this key can't read usage; that's fine
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()


def register_job(job_id, phase):
    state = {"cancelled": False, "conn": None, "phase": phase, "pct": 0, "error": None, "doc": None,
             "t0": time.time()}
    with JOBS_LOCK:
        JOBS[job_id] = state
    return state


def cancel_job(job_id):
    with JOBS_LOCK:
        state = JOBS.get(job_id)
    if not state:
        return
    state["cancelled"] = True
    proc = state.get("proc")
    if proc is not None:
        kill_tree(proc)
    conn = state.get("conn")
    if conn is not None and getattr(conn, "sock", None):
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


# ================================================================ web video support (yt-dlp add-on)

TOOLS = CONFIG_DIR / "tools"
YTDLP = TOOLS / "yt-dlp.exe"
QJS = TOOLS / "qjs.exe"
# QuickJS is the small JavaScript engine yt-dlp uses for YouTube's checks. Pinned, with its published digest.
QJS_URL = "https://github.com/quickjs-ng/quickjs/releases/download/v0.18.0/qjs-windows-x86_64.exe"
QJS_SHA256 = "ff03398d74ea6e6bd3a8e58058f7003225abcac229d7d57775b7aaae812a49c7"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
ADDON = {"phase": "idle", "pct": 0, "error": None}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MEDIA_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".amr", ".aif", ".aiff",
              ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg", ".3gp", ".ts", ".mts")


def addon_installed():
    return YTDLP.exists() and QJS.exists()


def addon_version():
    if not addon_installed():
        return None
    try:
        out = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=NO_WINDOW)
        return out.stdout.strip() or None
    except Exception:
        return None


def fetch_to(url, path, progress, base, share):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    sha = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            sha.update(chunk)
            got += len(chunk)
            if total:
                progress(base + share * got / total)
    return sha.hexdigest()


def install_addon():
    def work():
        try:
            ADDON.update(phase="downloading", pct=0, error=None)
            TOOLS.mkdir(parents=True, exist_ok=True)
            setp = lambda p: ADDON.__setitem__("pct", p)
            sums = urllib.request.urlopen(urllib.request.Request(YTDLP_SUMS, headers={"User-Agent": UA}), timeout=30).read().decode()
            want = next((l.split()[0] for l in sums.splitlines() if l.strip().endswith(" yt-dlp.exe")), None)
            tmp = TOOLS / "yt-dlp.exe.download"
            if fetch_to(YTDLP_URL, tmp, setp, 0, 0.92) != want:
                tmp.unlink(missing_ok=True)
                raise RuntimeError("The yt-dlp download didn't match its published checksum. Try again later.")
            os.replace(tmp, YTDLP)
            tmp = TOOLS / "qjs.exe.download"
            if fetch_to(QJS_URL, tmp, setp, 0.92, 0.08) != QJS_SHA256:
                tmp.unlink(missing_ok=True)
                raise RuntimeError("The QuickJS download didn't match its published checksum. Try again later.")
            os.replace(tmp, QJS)
            (TOOLS / "last_update.txt").write_text(str(time.time()))
            ADDON.update(phase="done", pct=1)
        except Exception as e:
            ADDON.update(phase="error", error="Couldn't install web video support: " + str(e))
    threading.Thread(target=work, daemon=True).start()


def update_addon(force=False):
    """yt-dlp updates itself (it checks its own signature). Once a day is plenty."""
    if not addon_installed():
        return
    stamp = TOOLS / "last_update.txt"
    try:
        last = float(stamp.read_text())
    except (OSError, ValueError):
        last = 0
    if not force and time.time() - last < 86400:
        return
    try:
        subprocess.run([str(YTDLP), "-U"], capture_output=True, timeout=180, creationflags=NO_WINDOW)
        stamp.write_text(str(time.time()))
    except Exception:
        pass


def kill_tree(proc):
    """yt-dlp.exe unpacks itself and runs as a child process, so stop the whole tree."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                       creationflags=NO_WINDOW)
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def ytdlp(state, args, on_line=None):
    cmd = [str(YTDLP), "--js-runtimes", f"quickjs:{QJS}", "--no-playlist", "--no-warnings",
           "--encoding", "utf-8", "--socket-timeout", "30", *args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    state["proc"] = p
    lines = []
    try:
        for line in p.stdout:
            if state["cancelled"]:
                kill_tree(p)
                raise Cancelled()
            line = line.rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        p.wait()
    finally:
        state["proc"] = None
    if state["cancelled"]:
        raise Cancelled()
    return p.returncode, lines


def is_meta(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(h in host for h in ("facebook.com", "fb.watch", "fb.com", "instagram.com"))


def needs_impersonation(url, lines):
    """Facebook answers plain requests with pages yt-dlp can't read; looking like Chrome sometimes helps."""
    low = "\n".join(lines).lower()
    return is_meta(url) and ("cannot parse data" in low or "impersonat" in low or "403" in low)


FIREFOX_PROFILES = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
SITE_NAMES = {"youtube": "YouTube", "youtubetab": "YouTube", "facebook": "Facebook", "instagram": "Instagram",
              "tiktok": "TikTok", "twitter": "X", "vimeo": "Vimeo", "soundcloud": "SoundCloud", "twitch": "Twitch",
              "rumble": "Rumble", "linkedin": "LinkedIn", "dailymotion": "Dailymotion", "reddit": "Reddit"}
# Speech transcribes as well from a ~50-80 kbps stream as from the best one, at well under half the size.
AUDIO_FORMAT = "ba[abr<=80]/wa/ba/b"


def firefox_available():
    try:
        return FIREFOX_PROFILES.exists() and any(FIREFOX_PROFILES.iterdir())
    except OSError:
        return False


def use_firefox(url):
    # Never for YouTube: signed-in downloads there can get the account flagged.
    return bool(prefs().get("firefoxCookies")) and firefox_available() and not is_youtube(url)


def is_login_wall(lines, url):
    low = "\n".join(lines).lower()
    return ((is_meta(url) and "cannot parse data" in low)
            or any(k in low for k in ("logged-in", "log in", "login", "cookies", "sign in", "registered users")))


def ytdlp_error(lines, url=""):
    text = "\n".join(l for l in lines if "ERROR" in l) or "\n".join(lines[-3:])
    low = text.lower()
    site = "Facebook" if "facebook" in url or "fb.watch" in url else "Instagram" if "instagram" in url else "This site"
    if "firefox" in low and ("cookie" in low or "could not find" in low):
        return "Couldn't read your Firefox sign-in. Open Firefox once, sign in to the site, then try again."
    if is_login_wall(lines, url) or "private" in low:
        if use_firefox(url):
            return (f"{site} didn't show this video even with your Firefox sign-in. Check you can play it in "
                    "Firefox, or download it yourself and drop the file in.")
        if firefox_available() and not is_youtube(url):
            return (f"{site} only shows this video to signed-in people. If you're signed in to {site if site != 'This site' else 'it'} "
                    "in Firefox, turn on \"Use my Firefox sign-in\" in Settings. Or download the video yourself and drop the file in.")
        return (f"{site} only shows this video to signed-in people, which Transcriber can't do here. "
                "Download the video yourself and drop the file in.")
    if "not a bot" in low:
        return ("YouTube is asking to confirm you're not a robot, which happens after many requests from one "
                "connection. Try again later, use YouTube's captions, or download the video and drop the file in.")
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only, so it can't be fetched."
    if "unsupported url" in low:
        return "Transcriber doesn't recognise a video on that page. Check the link points to a single video."
    if "age" in low and "confirm" in low:
        return "This video is age-restricted, so it can't be fetched without signing in."
    if "is not available" in low or "unavailable" in low or "404" in low or "blocked" in low:
        return "That video isn't available here. It may have been removed, or be blocked in your country."
    if "max-filesize" in low or "larger than max" in low:
        return "This video's audio is over 2 GB."
    m = re.search(r"ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[\w-]+:\s*)?(.*)", text)
    return "Couldn't fetch the video: " + (m.group(1).strip() if m else text.strip())[:300]


def is_youtube(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return host.endswith("youtube.com") or host.endswith("youtu.be") or host.endswith("youtube-nocookie.com")


def link_kind(url):
    """'share' (cloud share link), 'direct' (a media file) or 'web' (a page yt-dlp reads)."""
    host = urllib.parse.urlparse(url).netloc.lower()
    path = urllib.parse.urlparse(url).path.lower()
    if any(h in host for h in ("dropbox.com", "drive.google.com", "1drv.ms", "onedrive.live.com", "sharepoint.com")):
        return "share"
    if path.endswith(MEDIA_EXTS):
        return "direct"
    return "web"


def ytdlp_tries(url, run):
    """Run yt-dlp with fallbacks. run(extra) -> (code, lines). Returns (code, lines, extra that worked)."""
    code, lines = run([])
    if code == 0:
        return code, lines, []
    if is_youtube(url) and any("403" in l for l in lines):
        extra = ["--extractor-args", "youtube:player_client=mweb"]
        code, lines = run(extra)
        if code == 0:
            return code, lines, extra
    imp = ["--impersonate", "chrome"] if is_meta(url) else []
    if imp and needs_impersonation(url, lines):
        code2, lines2 = run(imp)
        if code2 == 0:
            return code2, lines2, imp
        code, lines = code2, lines2
    if use_firefox(url) and is_login_wall(lines, url):
        extra = [*imp, "--cookies-from-browser", "firefox"]
        code, lines = run(extra)
        if code == 0:
            return code, lines, extra
    return code, lines, None


INFO_CACHE = {}  # url -> (time, saved json path, info, extra args that worked)


def lookup_web(state, url):
    """Look a video up (yt-dlp -J), reusing a lookup from the last 30 minutes."""
    hit = INFO_CACHE.get(url)
    if hit and time.time() - hit[0] < 1800 and hit[1].exists():
        return hit[2], hit[1], hit[3]
    state["phase"] = "fetching"
    code, lines, extra = ytdlp_tries(url, lambda x: ytdlp(state, [*x, "-J", url]))
    raw = next((l for l in lines if l.startswith("{")), None)
    if code != 0 or not raw:
        raise DGError(ytdlp_error(lines, url))
    info = json.loads(raw)
    if info.get("_type") == "playlist":
        raise DGError("That's a playlist. Paste a link to a single video.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise DGError("That's a live stream that hasn't finished. Transcribe it once it has ended.")
    cache = TOOLS / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    path.write_text(raw, encoding="utf-8")
    INFO_CACHE[url] = (time.time(), path, info, extra or [])
    return info, path, extra or []


def summarize_info(info, url):
    date = info.get("upload_date") or ""
    subs, autos = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    fmts = [f for f in info.get("formats") or [] if f.get("acodec") not in (None, "none")]
    audio = [f for f in fmts if f.get("vcodec") in (None, "none")]
    small = [f for f in audio if (f.get("abr") or 999) <= 80]
    pick = max(small, key=lambda f: f.get("abr") or 0) if small else (min(audio, key=lambda f: f.get("abr") or 999) if audio else None)
    key = (info.get("extractor_key") or "").lower()
    return {
        "title": info.get("title") or info.get("fulltitle"),
        "uploader": info.get("channel") or info.get("uploader") or info.get("creator"),
        "uploaderUrl": info.get("channel_url") or info.get("uploader_url"),
        "duration": info.get("duration"),
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else None,
        "thumbnail": info.get("thumbnail"),
        "url": info.get("webpage_url") or url,
        "site": SITE_NAMES.get(key) or info.get("extractor_key") or urllib.parse.urlparse(url).netloc.replace("www.", ""),
        "views": info.get("view_count"),
        "description": (info.get("description") or "").strip()[:500],
        "language": ((info.get("language") or "").split("-")[0]) or None,
        "captions": {"manual": sorted({k.split("-")[0] for k in subs if k != "live_chat"}),
                     "auto": sorted({k[:-5] for k in autos if k.endswith("-orig")})},
        "audioSize": (pick or {}).get("filesize") or (pick or {}).get("filesize_approx"),
    }


def download_web_video(state, url, folder, info_path=None, extra0=None):
    """Fetch a small audio stream with yt-dlp. Returns (path, title, duration).
    With info_path (saved by -J), yt-dlp skips looking the video up again."""
    state["phase"] = "fetching"
    info = {"title": None, "file": None, "dur": None}

    def on_line(line):
        if line.startswith("TITLE "):
            info["title"] = line[6:].strip()
            state["phase"] = "downloading"
        elif line.startswith("DUR "):
            try:
                info["dur"] = float(line[4:])
            except ValueError:
                pass
        elif line.startswith("FILE "):
            info["file"] = line[5:].strip()
        elif line.startswith("PCT "):
            parts = line.split()
            try:
                got, total = float(parts[1]), float(parts[2])
                if total > 0:
                    state["phase"] = "downloading"
                    state["pct"] = min(1.0, got / total)
            except (ValueError, IndexError):
                pass

    def attempt(extra, saved=None):
        for f in folder.glob("media.*"):
            f.unlink(missing_ok=True)
        return ytdlp(state, [*extra, "-f", AUDIO_FORMAT, "--max-filesize", "2G", "--newline", "--no-part",
                             "--no-mtime", "--progress",
                             "--progress-template", "download:PCT %(progress.downloaded_bytes)s %(progress.total_bytes,progress.total_bytes_estimate)s",
                             "--print", "before_dl:TITLE %(title)s", "--print", "before_dl:DUR %(duration)s",
                             "--print", "after_move:FILE %(filepath)s",
                             "-o", str(folder / "media.%(ext)s"),
                             *(["--load-info-json", str(saved)] if saved else [url])], on_line)

    code, lines = (attempt(extra0 or [], info_path) if info_path else (1, []))
    if code != 0:  # no saved lookup, or its links expired: look the video up again
        INFO_CACHE.pop(url, None)
        code, lines, _ = ytdlp_tries(url, attempt)
    if code != 0 or not info["file"] or not Path(info["file"]).exists():
        raise DGError(ytdlp_error(lines, url))
    return Path(info["file"]), info["title"] or "Web video", info["dur"]


def save_thumbnail(doc, thumb_url):
    if not thumb_url or not thumb_url.startswith("https://"):
        return
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(3 * 1024 * 1024)
        if data[:3] == b"\xff\xd8\xff" or data[:4] in (b"\x89PNG", b"RIFF"):
            (doc_dir(doc["id"]) / "thumb.img").write_bytes(data)
            doc["thumb"] = True
    except Exception:
        pass


# ================================================================ YouTube captions

def pick_caption_track(info, language):
    subs, autos = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    video_lang = (info.get("language") or "").split("-")[0]
    want = (video_lang or "en") if language in ("auto", "multi", "", None) else language
    json3 = lambda tracks: next((t for t in tracks if t.get("ext") == "json3"), None)

    def first(d, keys, auto):
        for k in keys:
            if k in d and json3(d[k]):
                return json3(d[k]), k, auto
        return None

    same = lambda d, lang: [k for k in d if k.split("-")[0] == lang and k != "live_chat"]
    return (first(subs, [want] + same(subs, want), False)
            or first(autos, [f"{want}-orig"] + ([want] if video_lang in ("", want) else []), True)
            or (first(subs, [video_lang] + same(subs, video_lang), False) if video_lang else None)
            or first(autos, [k for k in autos if k.endswith("-orig")], True)
            or first(autos, [want], True))


def caption_words(j, auto):
    events = [e for e in j.get("events", []) if e.get("segs")]
    words = []
    for e in events:
        base = e.get("tStartMs", 0) / 1000
        end = base + e.get("dDurationMs", 0) / 1000
        if auto:
            for sg in e["segs"]:
                t = sg.get("utf8", "").strip()
                if t:
                    words.append({"w": t, "s": base + sg.get("tOffsetMs", 0) / 1000, "e": end, "c": 1})
        else:  # hand-made captions: spread each line's words over its time
            parts = " ".join("".join(sg.get("utf8", "") for sg in e["segs"]).split()).split()
            span = max(end - base, 0.2)
            for k, w in enumerate(parts):
                words.append({"w": w, "s": base + span * k / len(parts), "e": base + span * (k + 1) / len(parts), "c": 1})
    words.sort(key=lambda w: w["s"])
    for a, b in zip(words, words[1:]):  # a word ends when the next starts (caption lines overlap)
        a["e"] = max(a["s"] + 0.05, min(a["e"], b["s"]))
    return [w for w in words if w["w"] not in ("[Music]", "[Applause]", "[Laughter]")]


def run_captions_job(job_id, url, opts, state, folder):
    info, info_path, extra = lookup_web(state, url)
    pick = pick_caption_track(info, opts.get("language"))
    if not pick:
        raise DGError("This video has no captions. Choose Transcribe with Deepgram instead.")
    track, lang, auto = pick
    state["phase"] = "captions"
    j = json.loads(urllib.request.urlopen(urllib.request.Request(track["url"], headers={"User-Agent": "Mozilla/5.0"}),
                                          timeout=60).read())
    words = caption_words(j, auto)
    if not words:
        raise DGError("This video's captions are empty.")
    media_name = None
    if opts.get("keepMedia"):
        state["phase"], state["pct"] = "downloading", 0
        try:
            path, _, _ = download_web_video(state, url, folder, info_path, extra)
            media_name = path.name
        except DGError:
            media_name = None  # captions still work without playback
    segs = finish_segments(chunk_words(words, False))
    summary = summarize_info(info, url)
    doc = new_doc(job_id, summary["title"] or "YouTube video", opts, media_name, segs, info.get("duration"),
                  lang.split("-")[0])
    doc["title"] = summary["title"] or "YouTube video"
    doc.update(source="YouTube captions" + (" (automatic)" if auto else ""), sourceUrl=url, cost=0.0,
               sourceInfo=summary)
    save_thumbnail(doc, summary.get("thumbnail"))
    return doc


def preview_link(state, url):
    """What the person sees before transcribing a link."""
    kind = link_kind(url)
    out = {"kind": kind, "youtube": is_youtube(url)}
    if kind == "web":
        if not addon_installed():
            return dict(out, needsAddon=True)
        update_addon()
        info, _, _ = lookup_web(state, url)
        return dict(out, info=summarize_info(info, url))
    target = normalize_link(url)
    req = urllib.request.Request(target, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype.startswith("text/html"):
                raise DGError('That link opens a sign-in or preview page, not the file. Share it with '
                              '"Anyone with the link" and try again.')
            total = r.headers.get("Content-Range", "").rpartition("/")[2] or r.headers.get("Content-Length")
            name = name_from_response(r.geturl() or target, r)
    except urllib.error.HTTPError as e:
        raise DGError(f"The link didn't open (error {e.code}). Check it's shared with anyone who has the link.")
    except DGError:
        raise
    except Exception as e:
        raise DGError("Couldn't open the link: " + str(e))
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    site = {"dropbox.com": "Dropbox", "drive.google.com": "Google Drive", "1drv.ms": "OneDrive",
            "onedrive.live.com": "OneDrive"}.get(host, "SharePoint" if "sharepoint.com" in host else host)
    return dict(out, info={"title": name, "site": site, "url": url,
                           "size": int(total) if str(total).isdigit() else None, "type": ctype})


# ================================================================ links

def normalize_link(url):
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        raise DGError("Paste a full link that starts with https://")
    host = urllib.parse.urlparse(url).netloc.lower()
    if "1drv.ms" in host or "onedrive.live.com" in host:
        import base64
        token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content"
    if "sharepoint.com" in host and "download=1" not in url:
        return url + ("&" if "?" in url else "?") + "download=1"
    if "dropbox.com" in host:
        url = re.sub(r"([?&])dl=0", r"\1dl=1", url)
        if "dl=1" not in url:
            url += ("&" if "?" in url else "?") + "dl=1"
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?.*id=)([\w-]+)", url)
    if m:
        url = f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    return url


def name_from_response(url, resp):
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)', cd)
    if m:
        return urllib.parse.unquote(m.group(1)).strip()
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return name or "Linked recording"


class NeedsAddon(Exception):
    pass


def download_direct(state, url, folder):
    """Fetch a file link. Returns (path, name, ctype) or None when the link is a web page."""
    state["phase"] = "downloading"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        raise DGError(f"The link didn't open (error {e.code}). Check it's shared with anyone who has the link.")
    except Exception as e:
        raise DGError("Couldn't open the link: " + str(e))
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype.startswith("text/html"):
        resp.close()
        return None
    name = name_from_response(resp.geturl() or url, resp)
    ext = Path(name).suffix.lower()[:8] or mimetypes.guess_extension(ctype) or ".bin"
    if not Path(name).suffix:
        name += ext
    total = int(resp.headers.get("Content-Length") or 0)
    path = folder / f"media{ext}"
    got = 0
    with open(path, "wb") as out:
        while True:
            if state["cancelled"]:
                raise Cancelled()
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total:
                state["pct"] = got / total
            if got > 2 * 1024 ** 3:
                raise DGError("This file is over 2 GB.")
    resp.close()
    if got == 0:
        raise DGError("The link returned an empty file.")
    return path, name, ctype


def transcribe_local(state, key, path, name, opts, ctype=None):
    state["phase"], state["pct"] = "uploading", 0
    size = path.stat().st_size
    with open(path, "rb") as f:
        return deepgram_listen(state, key, dg_params(opts), TeeReader(f, size, None, state), size,
                               mimetypes.guess_type(name)[0] or ctype or "application/octet-stream")


def run_link_job(job_id, url, opts, state):
    folder = doc_dir(job_id)
    ok = False
    try:
        original = url.strip()
        url = normalize_link(original)
        folder.mkdir(parents=True, exist_ok=True)
        kind = link_kind(original)
        if opts.get("captions") and is_youtube(original):
            if not addon_installed():
                raise NeedsAddon()
            update_addon()
            doc = run_captions_job(job_id, original, opts, state, folder)
        else:
            key = get_key()
            if not key:
                raise DGError("Add your Deepgram API key in Settings first.")
            got, summary = None, None
            if kind != "web":
                got = download_direct(state, url, folder)
                if got is None and kind == "share":
                    raise DGError('That link opens a sign-in or preview page, not the file. Share it with '
                                  '"Anyone with the link" and try again.')
            if got:
                path, name, ctype = got
            else:
                if not addon_installed():
                    raise NeedsAddon()
                update_addon()
                info, info_path, extra = lookup_web(state, original)
                summary = summarize_info(info, original)
                path, name, _ = download_web_video(state, original, folder, info_path, extra)
                name = summary["title"] or name
                ctype = None
            dg = transcribe_local(state, key, path, name, opts, ctype)
            media_name = path.name if opts.get("keepMedia") else None
            if not media_name:
                path.unlink(missing_ok=True)
            write_json(folder / "raw.json", dg)
            doc = doc_from_deepgram(job_id, name, dg, opts, media_name)
            doc["sourceUrl"] = original
            if summary:
                doc["title"] = name  # a page title, not a file name: keep any dots in it
                doc["source"] = summary["site"]
                doc["sourceInfo"] = summary
                save_thumbnail(doc, summary.get("thumbnail"))
        save_doc(doc)
        if doc.get("requestId"):
            fetch_cost_later(job_id, doc["requestId"])
        state["doc"] = doc
        state["phase"] = "done"
        ok = True
    except Cancelled:
        state["phase"] = "cancelled"
    except NeedsAddon:
        state.update(phase="error", needsAddon=True,
                     error="This link needs web video support, which isn't installed yet.")
    except DGError as e:
        state["phase"], state["error"] = "error", str(e)
    except Exception as e:
        state["phase"], state["error"] = "error", "Something went wrong: " + str(e)
    finally:
        if not ok:
            shutil.rmtree(folder, ignore_errors=True)


# ================================================================ Gemini write-ups

WRITEUPS = {
    "summary": ("Summary and key points",
                "Write a short summary (one paragraph), then a '## Key points' section with 5 to 10 bullet points."),
    "chapters": ("Chapters",
                 "Split the recording into chapters where the topic changes. For each chapter write one bullet: "
                 "'- [mm:ss] Chapter title: one sentence on what is covered', using the time the chapter starts "
                 "from the transcript's timestamps. Use h:mm:ss for times past one hour."),
    "actions": ("Action items and decisions",
                "List '## Decisions' and '## Action items'. For each action item, say who will do it and by when "
                "if the transcript says so. If there are none, say so in one line."),
    "sermon": ("Sermon notes",
               "Write sermon notes: '## Title' (a fitting title), '## Scripture' (every Bible passage mentioned, "
               "with reference), '## Main points' (numbered, each with 1-3 sub-bullets), '## Key quotes' "
               "(short, exact quotes), and '## Application' (how listeners are asked to respond)."),
    "minutes": ("Meeting minutes",
                "Write meeting minutes: '## Attendees' (speaker names as they appear), '## Topics discussed' "
                "(a short paragraph per topic), '## Decisions', and '## Action items' (who, what, when)."),
    "custom": ("Custom write-up", ""),
}


def transcript_for_ai(doc):
    lines = []
    for s in doc["segments"]:
        if not s["text"].strip():
            continue
        name = speaker_name(doc, s)
        lines.append(f"[{clock(s['start'])}] " + (f"{name}: " if name else "") + s["text"].strip())
    return "\n".join(lines)


def gemini_generate(key, model, system, prompt):
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:
            msg = raw[:300]
        if e.code in (401, 403) or "API key" in msg:
            raise DGError("Google didn't accept the Gemini key. Check it in Settings.")
        if e.code == 429:
            raise DGError("Gemini's limit for this key has been reached. Wait a minute and try again.")
        raise DGError("Gemini couldn't write this: " + msg)
    except Exception as e:
        raise DGError("Couldn't reach Gemini. Check your internet connection. (" + str(e) + ")")
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise DGError("Gemini didn't return any text" + (f" ({reason})." if reason else "."))
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise DGError("Gemini returned an empty answer. Try again.")
    return text


def make_writeup(doc, kind, custom, english):
    key = get_key("gemini")
    if not key:
        raise DGError("Add a Gemini API key in Settings first.")
    title, task = WRITEUPS.get(kind, WRITEUPS["summary"])
    if kind == "custom":
        task = custom.strip() or "Summarise the transcript."
        title = (custom.strip().split("\n")[0][:60]) or title
    lang = "English" if english else "the same language as the transcript"
    system = ("You write clear, accurate notes from a transcript of a recording. Use only what is in the "
              "transcript and never invent names, facts or quotes. Write in " + lang + ". Format with "
              "Markdown using only '## ' headings, '- ' bullets, numbered lists and **bold**. "
              "Don't add a top-level title or any preamble.")
    prompt = f"{task}\n\nTranscript of \"{doc['title']}\" (each line starts with its time):\n\n{transcript_for_ai(doc)}"
    model = prefs().get("geminiModel") or GEMINI_MODELS[0]
    text = gemini_generate(key, model, system, prompt)
    return {"id": uuid.uuid4().hex[:10], "kind": kind, "title": title, "text": text,
            "created": time.time(), "model": model}


# ================================================================ exports

def clock(t, sep=None):
    ms = int(round((t or 0) * 1000))
    h, m, s = ms // 3600000, ms // 60000 % 60, ms // 1000 % 60
    if sep:
        return f"{h:02}:{m:02}:{s:02}{sep}{ms % 1000:03}"
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def speaker_name(doc, seg):
    if seg.get("speaker") is None:
        return None
    return doc["speakers"].get(str(seg["speaker"])) or f"Speaker {int(seg['speaker']) + 1}"


def clip_doc(doc, start, end, rebase):
    """A copy of the document holding only what was said between start and end."""
    if start is None and end is None:
        return doc
    a = start or 0
    b = end if end is not None else 10 ** 9
    out = dict(doc, segments=[])
    for s in doc["segments"]:
        if s.get("end", 0) < a or s.get("start", 0) > b:
            continue
        s = dict(s)
        if s.get("words") and not s.get("edited"):
            ws = [w for w in s["words"] if w["e"] > a and w["s"] < b]
            if not ws:
                continue
            s.update(words=ws, text=" ".join(w["w"] for w in ws), start=ws[0]["s"], end=ws[-1]["e"])
        if rebase:
            shift = lambda t: max(0.0, (t or 0) - a)
            s["start"], s["end"] = shift(s["start"]), shift(s["end"])
            if s.get("words"):
                s["words"] = [dict(w, s=shift(w["s"]), e=shift(w["e"])) for w in s["words"]]
        out["segments"].append(s)
    out["title"] = f"{doc['title']} ({clock(a)}–{clock(b) if end is not None else 'end'})"
    return out


def blocks(doc, stamps, speakers):
    out, last = [], object()
    for s in doc["segments"]:
        if not s["text"].strip():
            continue
        parts = []
        name = speaker_name(doc, s) if speakers else None
        if name and name != last:
            parts.append(name)
        if stamps:
            parts.append(clock(s["start"]))
        if speakers:
            last = name
        out.append((", ".join(parts), s["text"].strip()))
    return out


def md_lines(text):
    """Yield (kind, text) for the small Markdown subset write-ups use."""
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        m = re.match(r"\s*#{1,6}\s+(.*)", line)
        if m:
            yield "h", m.group(1)
            continue
        m = re.match(r"(\s*)[-*•]\s+(.*)", line)
        if m:
            yield ("li2" if len(m.group(1)) >= 2 else "li"), m.group(2)
            continue
        m = re.match(r"\s*(\d+[.)])\s+(.*)", line)
        if m:
            yield "ol", m.group(1) + " " + m.group(2)
            continue
        yield "p", line


def strip_md(t):
    return re.sub(r"\*\*(.+?)\*\*", r"\1", t)


def writeups_of(doc, include):
    return (doc.get("writeups") or []) if include else []


def export_txt(doc, o):
    out = []
    if o["writeups"]:
        for w in writeups_of(doc, True):
            out.append(w["title"].upper())
            for kind, t in md_lines(w["text"]):
                out.append({"h": "\n" + strip_md(t), "li": "  • " + strip_md(t), "li2": "      – " + strip_md(t),
                            "ol": "  " + strip_md(t)}.get(kind, strip_md(t)))
            out.append("")
        out.append("TRANSCRIPT\n")
    for head, text in blocks(doc, o["stamps"], o["speakers"]):
        out.append(f"[{head}]\n{text}\n" if head else text + "\n")
    return "\n".join(out)


def export_md(doc, o):
    out = [f"# {doc['title']}", ""]
    if doc.get("summary"):
        out += ["**Summary:** " + doc["summary"], ""]
    for w in writeups_of(doc, o["writeups"]):
        out += [f"## {w['title']}", "", re.sub(r"^#{1,6} ", "### ", w["text"], flags=re.M), ""]
    if o["writeups"] and doc.get("writeups"):
        out += ["## Transcript", ""]
    for head, text in blocks(doc, o["stamps"], o["speakers"]):
        if head:
            out.append(f"**{head}**  ")
        out += [text, ""]
    return "\n".join(out)


def cues(doc, speakers):
    result = []
    for s in doc["segments"]:
        text = s["text"].strip()
        if not text:
            continue
        name = speaker_name(doc, s) if speakers else None
        if s.get("words") and not s.get("edited"):
            chunk = []
            for w in s["words"]:
                chunk.append(w)
                if len(" ".join(x["w"] for x in chunk)) > 80 or w["e"] - chunk[0]["s"] > 7:
                    result.append((chunk[0]["s"], chunk[-1]["e"], " ".join(x["w"] for x in chunk), name))
                    chunk = []
            if chunk:
                result.append((chunk[0]["s"], chunk[-1]["e"], " ".join(x["w"] for x in chunk), name))
        else:
            pieces, cur = [], []
            for w in text.split():
                cur.append(w)
                if len(" ".join(cur)) > 80:
                    pieces.append(" ".join(cur))
                    cur = []
            if cur:
                pieces.append(" ".join(cur))
            span = max((s["end"] or 0) - (s["start"] or 0), 0.5 * len(pieces))
            total = sum(len(p) for p in pieces) or 1
            t = s["start"] or 0
            for p in pieces:
                d = span * len(p) / total
                result.append((t, t + d, p, name))
                t += d
    return result


def wrap_line(t):
    if len(t) <= 42:
        return t
    mid = t.rfind(" ", 0, len(t) // 2 + 6)
    return t[:mid] + "\n" + t[mid + 1:] if mid > 0 else t


def export_subs(doc, speakers, vtt):
    out = ["WEBVTT", ""] if vtt else []
    for i, (a, b, text, name) in enumerate(cues(doc, speakers), 1):
        if name:
            text = f"[{name}] {text}"
        if not vtt:
            out.append(str(i))
        sep = "." if vtt else ","
        out += [f"{clock(a, sep)} --> {clock(b, sep)}", wrap_line(text), ""]
    return "\n".join(out)


def doc_heading_bits(doc):
    meta = []
    if doc.get("duration"):
        meta.append("Length " + clock(doc["duration"]))
    meta.append(time.strftime("%d %B %Y", time.localtime(doc["created"])))
    return ", ".join(meta)


def docx_bytes(doc, o):
    def run(text, bold=False, size=None, color=None):
        props = ""
        if bold or size or color:
            props = "<w:rPr>" + ("<w:b/>" if bold else "") + \
                    (f'<w:color w:val="{color}"/>' if color else "") + \
                    (f'<w:sz w:val="{size}"/>' if size else "") + "</w:rPr>"
        return f'<w:r>{props}<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'

    def rich(text, size=None):  # **bold** spans
        parts = re.split(r"(\*\*.+?\*\*)", text)
        return "".join(run(p[2:-2], bold=True, size=size) if p.startswith("**") and p.endswith("**")
                       else run(p, size=size) for p in parts if p)

    def para(runs, after=160, indent=0):
        ind = f'<w:ind w:left="{indent}" w:hanging="{240 if indent else 0}"/>' if indent else ""
        return f'<w:p><w:pPr><w:spacing w:after="{after}"/>{ind}</w:pPr>{runs}</w:p>'

    body = [para(run(doc["title"], bold=True, size=36), after=80),
            para(run(doc_heading_bits(doc), color="666666", size=20), after=320)]
    if doc.get("summary"):
        body += [para(run("Summary", bold=True)), para(run(doc["summary"]), after=320)]
    for w in writeups_of(doc, o["writeups"]):
        body.append(para(run(w["title"], bold=True, size=28, color="1F4E79"), after=120))
        for kind, t in md_lines(w["text"]):
            if kind == "h":
                body.append(para(run(strip_md(t), bold=True, size=24), after=80))
            elif kind in ("li", "li2"):
                body.append(para(run("•\t") + rich(t), after=60, indent=480 if kind == "li" else 960))
            elif kind == "ol":
                body.append(para(rich(t), after=60, indent=480))
            else:
                body.append(para(rich(t)))
        body.append(para("", after=200))
    if o["writeups"] and doc.get("writeups"):
        body.append(para(run("Transcript", bold=True, size=28, color="1F4E79"), after=120))
    for head, text in blocks(doc, o["stamps"], o["speakers"]):
        if head:
            body.append(para(run(head, bold=True, color="1F4E79", size=20), after=40))
        body.append(para(run(text)))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>' + "".join(body) +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
                '</w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
              '<w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>')
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{rel}">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/_rels/document.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{rel}">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()


def pdf_bytes(doc, o):
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, 18)
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    regular, bold = fonts / "segoeui.ttf", fonts / "segoeuib.ttf"
    if regular.exists() and bold.exists():
        pdf.add_font("UI", "", str(regular))
        pdf.add_font("UI", "B", str(bold))
        face, clean = "UI", (lambda t: t)
    else:
        face, clean = "Helvetica", (lambda t: t.encode("latin-1", "replace").decode("latin-1"))
    pdf.add_page()
    w = pdf.epw

    def write(text, size=11, style="", color=(24, 32, 43), gap=2.0, indent=0):
        pdf.set_font(face, style, size)
        pdf.set_text_color(*color)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(w - indent, size * 0.5, clean(text), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(gap)

    write(doc["title"], 18, "B", gap=1)
    write(doc_heading_bits(doc), 9.5, color=(110, 118, 130), gap=6)
    if doc.get("summary"):
        write("Summary", 11, "B", gap=1)
        write(doc["summary"], gap=6)
    for wu in writeups_of(doc, o["writeups"]):
        write(wu["title"], 13.5, "B", color=(27, 94, 122), gap=2)
        for kind, t in md_lines(wu["text"]):
            t = strip_md(t)
            if kind == "h":
                write(t, 11.5, "B", gap=1)
            elif kind in ("li", "li2"):
                write("•  " + t, indent=5 if kind == "li" else 11, gap=1)
            elif kind == "ol":
                write(t, indent=5, gap=1)
            else:
                write(t)
        pdf.ln(4)
    if o["writeups"] and doc.get("writeups"):
        write("Transcript", 13.5, "B", color=(27, 94, 122), gap=2)
    for head, text in blocks(doc, o["stamps"], o["speakers"]):
        if head:
            write(head, 9.5, "B", color=(27, 94, 122), gap=0.5)
        write(text, gap=3)
    return bytes(pdf.output())


EXPORTS = {"docx": "Word document", "pdf": "PDF", "txt": "Text", "srt": "Subtitles",
           "vtt": "Web subtitles", "md": "Markdown", "json": "Data"}


def export_opts(get):
    num = lambda k: float(get(k)) if get(k) not in (None, "", "null") else None
    return {"stamps": str(get("stamps")) in ("1", "True", "true"),
            "speakers": str(get("speakers", "1")) in ("1", "True", "true"),
            "writeups": str(get("writeups")) in ("1", "True", "true"),
            "start": num("start"), "end": num("end"),
            "rebase": str(get("rebase")) in ("1", "True", "true")}


def build_export(doc, fmt, o):
    doc = clip_doc(doc, o["start"], o["end"], o["rebase"])
    if fmt == "docx":
        return docx_bytes(doc, o)
    if fmt == "pdf":
        return pdf_bytes(doc, o)
    if fmt == "txt":
        text = export_txt(doc, o)
    elif fmt == "md":
        text = export_md(doc, o)
    elif fmt == "srt":
        text = export_subs(doc, o["speakers"], vtt=False)
    elif fmt == "vtt":
        text = export_subs(doc, o["speakers"], vtt=True)
    elif fmt == "json":
        text = json.dumps(doc, ensure_ascii=False, indent=2)
    else:
        raise ValueError("Unknown format")
    return ("\ufeff" + text if fmt in ("txt", "srt") else text).encode("utf-8")


def file_base(title):
    return re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:120] or "Transcript"


def export_name(doc, fmt, o):
    base = file_base(doc["title"])
    if o["start"] is not None or o["end"] is not None:
        base += f" ({clock(o['start'] or 0).replace(':', '.')}-{clock(o['end']).replace(':', '.') if o['end'] is not None else 'end'})"
    return f"{base}.{fmt}"


def build_zip(ids, fmt, o):
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in ids:
            doc = load_doc(i)
            if not doc:
                continue
            name = export_name(doc, fmt, o)
            n = 2
            while name.lower() in used:
                name = f"{file_base(doc['title'])} ({n}).{fmt}"
                n += 1
            used.add(name.lower())
            z.writestr(name, build_export(doc, fmt, o))
    return buf.getvalue()


# ================================================================ updates

def parse_version(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:-([a-z]+)\.?(\d*))?", (v or "").strip().lower())
    if not m:
        return (0, 0, 0, 0, 0)
    pre = m.group(4)
    # A full release sorts after its own betas.
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, int(m.group(5) or 0))


def check_update():
    # latest.json in the repo is served from GitHub's file CDN, which has no per-computer limit.
    # The releases API is the fallback (it allows 60 checks an hour per internet connection).
    try:
        req = urllib.request.Request(f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/latest.json?t={int(time.time())}",
                                     headers={"User-Agent": UA, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as r:
            m = json.loads(r.read())
        return {"current": VERSION, "latest": m["version"],
                "available": parse_version(m["version"]) > parse_version(VERSION),
                "notes": (m.get("notes") or "").strip()[:2000], "url": m["url"], "size": m.get("size"),
                "digest": ("sha256:" + m["sha256"]) if m.get("sha256") else None, "canInstall": FROZEN}
    except Exception:
        pass
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases?per_page=10",
                                 headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        releases = json.loads(r.read())
    for rel in releases:
        if rel.get("draft"):
            continue
        asset = next((a for a in rel.get("assets", []) if re.match(r"Transcriber-Setup-.*\.exe$", a["name"])), None)
        if not asset:
            continue
        latest = rel["tag_name"].lstrip("v")
        return {"current": VERSION, "latest": latest,
                "available": parse_version(latest) > parse_version(VERSION),
                "notes": (rel.get("body") or "").strip()[:2000], "url": asset["browser_download_url"],
                "size": asset.get("size"), "digest": asset.get("digest"), "canInstall": FROZEN}
    return {"current": VERSION, "latest": None, "available": False, "canInstall": FROZEN}


def install_update(info):
    def work():
        try:
            if not info["url"].startswith(f"https://github.com/{UPDATE_REPO}/releases/download/"):
                raise RuntimeError("The update link doesn't point to Transcriber's releases.")
            UPDATE.update(phase="downloading", pct=0, error=None)
            path = Path(tempfile.gettempdir()) / f"Transcriber-Setup-{info['latest']}.exe"
            req = urllib.request.Request(info["url"], headers={"User-Agent": UA})
            sha = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as out:
                total = int(r.headers.get("Content-Length") or info.get("size") or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    got += len(chunk)
                    UPDATE["pct"] = got / total if total else 0
            digest = info.get("digest") or ""
            if digest.startswith("sha256:") and digest[7:].lower() != sha.hexdigest():
                path.unlink(missing_ok=True)
                raise RuntimeError("The download didn't match the published file. Try again later.")
            UPDATE["phase"] = "installing"
            subprocess.Popen([str(path), "/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART",
                              "/CLOSEAPPLICATIONS"], close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            time.sleep(1.5)
            os._exit(0)
        except Exception as e:
            UPDATE.update(phase="error", error=str(e))
    threading.Thread(target=work, daemon=True).start()


# ================================================================ HTTP

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/icon.png": ("icon.png", "image/png")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # The packaged app has no console (sys.stdout is None), so only log when there is one.
        if sys.stdout:
            sys.stdout.write("  " + (fmt % args) + "\n")

    def send_bytes(self, status, body, ctype, extra=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def route(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        return url.path, url.path.strip("/").split("/"), (lambda k, d=None: q.get(k, [d])[0])

    # -------------------------------------------------------- GET
    def do_GET(self):
        path, parts, arg = self.route()
        if path in STATIC:
            name, ctype = STATIC[path]
            p = HERE / name
            if not p.exists():
                p = HERE / "build-assets" / name
            return self.send_bytes(200, p.read_bytes(), ctype)
        if path == "/api/status":
            return self.send_json(200, {"hasKey": bool(get_key()), "hasGemini": bool(get_key("gemini")),
                                        "version": VERSION, "library": str(LIBRARY), "frozen": FROZEN,
                                        "geminiModels": GEMINI_MODELS,
                                        "addon": {"installed": addon_installed()},
                                        "firefox": firefox_available()})
        if path == "/api/prefs":
            return self.send_json(200, prefs())
        if path == "/api/library":
            q = arg("q", "") or ""
            return self.send_json(200, search_library(q) if q.strip() else all_metas())
        if parts[:2] == ["api", "library"] and len(parts) == 3:
            doc = load_doc(parts[2])
            return self.send_json(200, doc) if doc else self.send_json(404, {"error": "This transcript no longer exists."})
        if parts[:2] == ["api", "jobs"] and len(parts) == 3:
            with JOBS_LOCK:
                st = JOBS.get(parts[2])
            if not st:
                return self.send_json(404, {"error": "Unknown job"})
            out = {k: st.get(k) for k in ("phase", "pct", "error", "doc", "needsAddon")}
            out["elapsed"] = time.time() - st.get("t0", time.time())
            if st["phase"] in ("done", "error", "cancelled"):
                with JOBS_LOCK:
                    JOBS.pop(parts[2], None)
            return self.send_json(200, out)
        if parts[:2] == ["api", "export"] and len(parts) == 3:
            doc = load_doc(parts[2])
            fmt = arg("fmt", "txt")
            if not doc or fmt not in EXPORTS:
                return self.send_json(404, {"error": "Not found"})
            o = export_opts(arg)
            return self.send_bytes(200, build_export(doc, fmt, o), "application/octet-stream", {
                "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(export_name(doc, fmt, o))})
        if path == "/api/export-many":
            fmt = arg("fmt", "txt")
            ids = [i for i in (arg("ids", "") or "").split(",") if safe_id(i)]
            if fmt not in EXPORTS or not ids:
                return self.send_json(400, {"error": "Choose transcripts to export."})
            return self.send_bytes(200, build_zip(ids, fmt, export_opts(arg)), "application/zip", {
                "Content-Disposition": "attachment; filename=Transcripts.zip"})
        if path == "/api/account":
            key = get_key()
            if not key:
                return self.send_json(200, {"error": "no key"})
            try:
                pid = project_id(key)
                bal = dg_get(f"/v1/projects/{pid}/balances", key).get("balances") or []
                usd = sum(float(b.get("amount", 0)) for b in bal if (b.get("units") or "usd").lower() == "usd")
                return self.send_json(200, {"balance": usd, "units": "usd"})
            except Exception as e:
                return self.send_json(200, {"error": str(e)})
        if path == "/api/live/token":
            key = get_key()
            if not key:
                return self.send_json(400, {"error": "Add your Deepgram API key in Settings first."})
            try:
                req = urllib.request.Request("https://" + DG_HOST + "/v1/auth/grant", data=b"{}", method="POST",
                                             headers={"Authorization": "Token " + key, "Content-Type": "application/json",
                                                      "User-Agent": UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    tok = json.loads(r.read())["access_token"]
                return self.send_json(200, {"kind": "bearer", "token": tok})
            except Exception:
                # Keys without permission to make short-lived tokens still work directly.
                return self.send_json(200, {"kind": "token", "token": key})
        if path == "/api/update/check":
            try:
                return self.send_json(200, check_update())
            except Exception as e:
                return self.send_json(200, {"error": "Couldn't check for updates: " + str(e), "current": VERSION})
        if path == "/api/addon":
            return self.send_json(200, dict(ADDON, installed=addon_installed(), version=addon_version()))
        if path == "/api/link-info":
            url = (arg("url", "") or "").strip()
            return self.send_json(200, {"kind": link_kind(url), "youtube": is_youtube(url),
                                        "needsAddon": link_kind(url) == "web" and not addon_installed()})
        if path == "/api/update/status":
            return self.send_json(200, UPDATE)
        if parts[0] == "thumb" and len(parts) == 2 and safe_id(parts[1]):
            p = doc_dir(parts[1]) / "thumb.img"
            if not p.exists():
                return self.send_json(404, {"error": "No picture"})
            data = p.read_bytes()
            ctype = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[:4] == b"RIFF" else "image/jpeg"
            return self.send_bytes(200, data, ctype)
        if parts[0] == "media" and len(parts) == 2 and safe_id(parts[1]):
            return self.serve_media(parts[1])
        self.send_json(404, {"error": "Not found"})

    do_HEAD = do_GET

    def serve_media(self, doc_id):
        path = media_path(doc_id)
        if not path:
            return self.send_json(404, {"error": "No media"})
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".webm" and ctype.startswith("video"):
            ctype = "audio/webm"
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
        length = max(0, end - start + 1)
        try:
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass

    # -------------------------------------------------------- PUT / DELETE
    def update_doc(self, doc_id, data):
        doc = load_doc(doc_id)
        if not doc:
            return self.send_json(404, {"error": "This transcript no longer exists."})
        for k in ("title", "speakers", "segments", "summary", "bookmarks", "writeups", "folder"):
            if k in data:
                doc[k] = data[k]
        doc["title"] = (doc["title"] or "").strip() or "Untitled"
        save_doc(doc)
        DIRTY.pop(doc_id, None)
        return self.send_json(200, meta_for(doc))

    def do_PUT(self):
        path, parts, arg = self.route()
        if parts[:2] == ["api", "library"] and len(parts) == 3 and safe_id(parts[2]):
            return self.update_doc(parts[2], self.body_json())
        if parts[:2] == ["api", "library"] and len(parts) == 4 and parts[3] == "media" and safe_id(parts[2]):
            doc = load_doc(parts[2])
            length = int(self.headers.get("Content-Length") or 0)
            if not doc or length <= 0:
                self.close_connection = True
                return self.send_json(400, {"error": "Nothing to save."})
            ext = re.sub(r"[^.a-z0-9]", "", (arg("ext", ".webm") or ".webm").lower())[:8] or ".webm"
            target = doc_dir(doc["id"]) / f"media{ext}"
            state = {"cancelled": False, "phase": "saving", "pct": 0}
            with open(target, "wb") as out:
                reader = TeeReader(self.rfile, length, out, state)
                while reader.read(1 << 16):
                    pass
            doc["media"] = target.name
            save_doc(doc)
            return self.send_json(200, {"ok": True})
        if path == "/api/prefs":
            p = prefs()
            p.update(self.body_json())
            write_json(PREFS_FILE, p)
            return self.send_json(200, p)
        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        path, parts, arg = self.route()
        if parts[:2] == ["api", "library"] and len(parts) == 3 and safe_id(parts[2]):
            shutil.rmtree(doc_dir(parts[2]), ignore_errors=True)
            return self.send_json(200, {"ok": True})
        if path == "/api/key":
            KEY_FILES.get(arg("kind", "gemini"), KEY_FILES["gemini"]).unlink(missing_ok=True)
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "Not found"})

    # -------------------------------------------------------- POST
    def do_POST(self):
        path, parts, arg = self.route()
        if path == "/api/key":
            data = self.body_json()
            key, kind = (data.get("key") or "").strip(), data.get("kind") or "deepgram"
            if kind not in KEY_FILES or not key:
                return self.send_json(400, {"error": "Paste your API key first."})
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            KEY_FILES[kind].write_text(key, encoding="utf-8")
            PROJECT["id"] = None
            return self.send_json(200, {"ok": True})
        if parts[:2] == ["api", "cancel"] and len(parts) == 3:
            cancel_job(parts[2])
            return self.send_json(200, {"ok": True})
        if path == "/api/dirty":
            doc_id = safe_id(self.body_json().get("id"))
            if doc_id:
                DIRTY[doc_id] = time.time()
            return self.send_json(200, {"ok": True})
        if parts[:2] == ["api", "library"] and len(parts) == 4 and parts[3] == "save" and safe_id(parts[2]):
            return self.update_doc(parts[2], self.body_json())
        if path == "/api/transcribe":
            return self.transcribe_upload(arg)
        if path == "/api/transcribe-url":
            data = self.body_json()
            job_id = safe_id(data.get("id")) or uuid.uuid4().hex
            state = register_job(job_id, "starting")
            threading.Thread(target=run_link_job, args=(job_id, data.get("url", ""), clean_opts(data.get("opts") or {}), state),
                             daemon=True).start()
            return self.send_json(200, {"id": job_id})
        if path == "/api/link-preview":
            data = self.body_json()
            job_id = safe_id(data.get("id")) or uuid.uuid4().hex
            state = register_job(job_id, "fetching")
            try:
                return self.send_json(200, preview_link(state, (data.get("url") or "").strip()))
            except Cancelled:
                return self.send_json(499, {"error": "Cancelled", "cancelled": True})
            except DGError as e:
                return self.send_json(502, {"error": str(e)})
            except Exception as e:
                return self.send_json(502, {"error": "Couldn't look the link up: " + str(e)})
            finally:
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
        if path == "/api/live/save":
            return self.live_save(self.body_json())
        if path == "/api/writeup":
            data = self.body_json()
            doc = load_doc(data.get("id"))
            if not doc:
                return self.send_json(404, {"error": "This transcript no longer exists."})
            try:
                wu = make_writeup(doc, data.get("kind", "summary"), data.get("custom", ""), bool(data.get("english")))
            except DGError as e:
                return self.send_json(502, {"error": str(e)})
            doc = load_doc(data.get("id")) or doc  # pick up edits saved while Gemini was writing
            doc.setdefault("writeups", []).insert(0, wu)
            save_doc(doc)
            return self.send_json(200, wu)
        if path == "/api/bulk/delete":
            for i in self.body_json().get("ids", []):
                if safe_id(i):
                    shutil.rmtree(doc_dir(i), ignore_errors=True)
            return self.send_json(200, {"ok": True})
        if path == "/api/bulk/move":
            data = self.body_json()
            for i in data.get("ids", []):
                doc = load_doc(i)
                if doc:
                    doc["folder"] = data.get("folder") or ""
                    save_doc(doc)
            return self.send_json(200, {"ok": True})
        if path == "/api/folders/rename":
            data = self.body_json()
            old, new = data.get("from") or "", (data.get("to") or "").strip()
            for m in all_metas():
                if m.get("folder") == old:
                    doc = load_doc(m["id"])
                    if doc:
                        doc["folder"] = new
                        save_doc(doc)
            return self.send_json(200, {"ok": True})
        if path == "/api/addon/install":
            if ADDON["phase"] != "downloading":
                install_addon()
            return self.send_json(200, {"ok": True})
        if path == "/api/addon/update":
            update_addon(force=True)
            return self.send_json(200, {"version": addon_version()})
        if path == "/api/addon/remove":
            shutil.rmtree(TOOLS, ignore_errors=True)
            ADDON.update(phase="idle", pct=0, error=None)
            return self.send_json(200, {"ok": True})
        if path == "/api/update/install":
            if not FROZEN:
                return self.send_json(400, {"error": "Updates install only in the installed app."})
            try:
                info = check_update()
            except Exception as e:
                return self.send_json(502, {"error": "Couldn't check for updates: " + str(e)})
            if not info.get("available"):
                return self.send_json(400, {"error": "You already have the latest version."})
            install_update(info)
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "Not found"})

    def transcribe_upload(self, arg):
        length = int(self.headers.get("Content-Length") or 0)
        job_id = safe_id(arg("id")) or uuid.uuid4().hex
        key = get_key()
        if not key:
            self.close_connection = True
            return self.send_json(400, {"error": "Add your Deepgram API key in Settings first."})
        if length <= 0:
            return self.send_json(400, {"error": "This file is empty."})
        opts = clean_opts({k: arg(k) for k in ("language", "model", "diarize", "filler", "profanity", "redact",
                                                "summary", "keepMedia", "folder")} |
                          {"keyterms": (arg("keyterms", "") or "").splitlines()})
        name = arg("name", "Recording") or "Recording"
        ext = (Path(name).suffix or ".bin").lower()[:8]
        folder = doc_dir(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        media_name = f"media{ext}" if opts["keepMedia"] else None
        state = register_job(job_id, "uploading")
        out = open(folder / media_name, "wb") if media_name else None
        ok = False
        try:
            dg = deepgram_listen(state, key, dg_params(opts), TeeReader(self.rfile, length, out, state), length,
                                 self.headers.get("Content-Type") or "application/octet-stream")
            if out:
                out.close()
                out = None
            write_json(folder / "raw.json", dg)
            doc = doc_from_deepgram(job_id, name, dg, opts, media_name)
            save_doc(doc)
            fetch_cost_later(job_id, doc.get("requestId"))
            ok = True
            self.send_json(200, doc)
        except Cancelled:
            self.close_connection = True
            self.send_json(499, {"error": "Cancelled", "cancelled": True})
        except DGError as e:
            self.close_connection = True
            self.send_json(502, {"error": str(e)})
        except Exception as e:
            self.close_connection = True
            self.send_json(502, {"error": "Something went wrong: " + str(e)})
        finally:
            if out:
                out.close()
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
            if not ok:
                shutil.rmtree(folder, ignore_errors=True)

    def live_save(self, data):
        doc_id = safe_id(data.get("id")) or uuid.uuid4().hex
        opts = clean_opts(data.get("opts") or {})
        segs = finish_segments(chunk_words(data.get("words") or [], opts["diarize"]))
        doc = new_doc(doc_id, data.get("title") or "Recording", opts, None, segs,
                      data.get("duration"), data.get("language") or opts["language"],
                      request_id=data.get("requestId"))
        doc["title"] = (data.get("title") or "Recording").strip()
        doc["source"] = "Microphone"
        save_doc(doc)
        fetch_cost_later(doc_id, doc.get("requestId"))
        return self.send_json(200, doc)


def clean_opts(o):
    truthy = lambda v: str(v).lower() in ("1", "true", "yes", "on")
    terms = o.get("keyterms") or []
    if isinstance(terms, str):
        terms = terms.splitlines()
    return {"language": o.get("language") or "en", "model": o.get("model") or "nova-3",
            "diarize": truthy(o.get("diarize")), "filler": truthy(o.get("filler")),
            "profanity": truthy(o.get("profanity")), "redact": truthy(o.get("redact")),
            "summary": truthy(o.get("summary")), "keepMedia": o.get("keepMedia") in (None, "") or truthy(o.get("keepMedia")),
            "captions": truthy(o.get("captions")),
            "folder": o.get("folder") or "",
            "keyterms": [t.strip() for t in terms if t and t.strip()][:100]}


# ================================================================ desktop window

class DesktopApi:
    """Called from the page as window.pywebview.api.* when running as the desktop app."""

    _window = None  # private: pywebview must not walk into the window object

    def _save_dialog(self, name, label, ext):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None) or webview.SAVE_DIALOG
        picked = self._window.create_file_dialog(kind, save_filename=name,
                                                 file_types=(f"{label} (*.{ext})", "All files (*.*)"))
        if not picked:
            return None
        path = picked if isinstance(picked, str) else picked[0]
        return path if path.lower().endswith("." + ext) else path + "." + ext

    def export(self, doc_id, fmt, opts):
        doc = load_doc(doc_id)
        if not doc or fmt not in EXPORTS:
            return {"error": "This transcript no longer exists."}
        o = export_opts(lambda k, d=None: (opts or {}).get(k, d))
        path = self._save_dialog(export_name(doc, fmt, o), EXPORTS[fmt], fmt)
        if not path:
            return {"saved": False}
        try:
            Path(path).write_bytes(build_export(doc, fmt, o))
        except OSError as e:
            return {"error": "Couldn't save the file: " + str(e)}
        return {"saved": True, "path": path}

    def export_many(self, ids, fmt, opts):
        o = export_opts(lambda k, d=None: (opts or {}).get(k, d))
        path = self._save_dialog("Transcripts.zip", "Zip archive", "zip")
        if not path:
            return {"saved": False}
        try:
            Path(path).write_bytes(build_zip([i for i in ids if safe_id(i)], fmt, o))
        except OSError as e:
            return {"error": "Couldn't save the file: " + str(e)}
        return {"saved": True, "path": path}

    def reveal(self, path):
        if path and Path(path).exists():
            subprocess.Popen(["explorer", "/select,", str(Path(path))])

    def open_library(self):
        LIBRARY.mkdir(parents=True, exist_ok=True)
        os.startfile(LIBRARY)

    def open_url(self, url):
        if re.match(r"https://", url or ""):
            webbrowser.open(url)


def wait_for_saves():
    """When the window closes, give the page a moment to send any edit it hasn't saved yet."""
    deadline = time.time() + 2.5
    while DIRTY and time.time() < deadline:
        time.sleep(0.05)
    return True


def main():
    threading.Thread(target=update_addon, daemon=True).start()
    browser_mode = "--browser" in sys.argv
    server = ThreadingHTTPServer(("127.0.0.1", PORT if browser_mode else 0), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}"

    if not browser_mode:
        try:
            import webview
            threading.Thread(target=server.serve_forever, daemon=True).start()
            api = DesktopApi()
            api._window = webview.create_window(
                "Transcriber Beta", url, js_api=api, width=1280, height=840, min_size=(780, 560))
            api._window.events.closing += wait_for_saves
            webview.start()
            return
        except Exception as e:  # no WebView2 on this PC: fall back to the browser
            if sys.stdout:
                print("Couldn't open the app window (" + str(e) + "); using the browser instead.")

    if sys.stdout:
        print(f"Transcriber is running at {url}")
        print("Keep this window open while you use it. Close it to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
