#!/usr/bin/env python3
"""ghostty-translate — drag-select-to-translate companion for Ghostty (any terminal).

Watches the PRIMARY selection (set by Ghostty whenever you drag-select text),
translates it, and shows a popup near the mouse — like zotero-pdf-translate.

Modes:
  default            daemon: watch selection -> translate -> popup
  --once "text"      translate text once, print result, exit
  --popup ...        internal: show the popup window (spawned by the daemon)

Config (environment variables):
  GTRANS_PRIMARY_TARGET    target lang for non-CJK text   (default zh-CN)
  GTRANS_SECONDARY_TARGET  target lang for CJK text       (default en)
  GTRANS_TTL               popup auto-close seconds       (default 8)
  GTRANS_MIN_CHARS         minimum selection length       (default 2)
  GTRANS_MAX_CHARS         maximum selection length       (default 1500)
  GTRANS_BACKENDS          comma list: llm,mymemory,libre,google
  GTRANS_LLM_ENDPOINT      OpenAI-compatible endpoint (base URL or /chat/completions)
  GTRANS_LLM_KEY           API key (Bearer)
  GTRANS_LLM_MODEL         model name
  GTRANS_LLM_PROMPT        prompt template with ${langFrom} ${langTo} ${sourceText}
  GTRANS_LLM_TEMP          sampling temperature (default 0.3)
  GTRANS_CONFIG            LLM config file (default ~/.config/ghostty-translate/llm.json)
  GTRANS_LIBRE_URL         LibreTranslate endpoint        (default disroot)
  GTRANS_EMAIL             MyMemory email (raises free quota)
  GTRANS_POPUP_CMD         custom popup: {} replaced by JSON {orig,trans}
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

TARGET_ZH = os.environ.get("GTRANS_PRIMARY_TARGET", "zh-CN")
TARGET_EN = os.environ.get("GTRANS_SECONDARY_TARGET", "en")
TTL = int(os.environ.get("GTRANS_TTL", "8"))
MIN_CHARS = int(os.environ.get("GTRANS_MIN_CHARS", "2"))
MAX_CHARS = int(os.environ.get("GTRANS_MAX_CHARS", "1500"))
LIBRE_URL = os.environ.get("GTRANS_LIBRE_URL", "https://translate.disroot.org/translate")
MYMEMORY_EMAIL = os.environ.get("GTRANS_EMAIL", "")
BACKENDS = [b.strip() for b in
            os.environ.get("GTRANS_BACKENDS", "llm,mymemory,libre,google").split(",")
            if b.strip()]
POPUP_CMD = os.environ.get("GTRANS_POPUP_CMD", "")
# "ghostty" (default): only translate selections Ghostty made, detected by its
# copy-on-select=clipboard dual write (PRIMARY + CLIPBOARD). "none": any app.
FILTER_MODE = os.environ.get("GTRANS_FILTER", "ghostty").strip().lower()
# Popup placement: "bottom-center"/"top-center" mirror altgo's overlay (primary
# monitor, horizontally centered, EDGE_OFFSET=80 logical px); "anchor" follows
# the selection.
POSITION_MODE = os.environ.get("GTRANS_POSITION", "bottom-center").strip().lower()

# LLM backend (OpenAI-compatible chat completions, like zotero-pdf-translate's
# GPT / Custom GPT services). Config: env vars, or a JSON file whose defaults
# are loaded first and overridden by env. Keys: endpoint, key, model, prompt,
# temperature, timeout.
DEFAULT_LLM_PROMPT = (
    "As an expert translator, translate the following text from ${langFrom} to "
    "${langTo}. Keep technical terms and proper names accurate. Reply with only "
    "the translation, no explanations or quotes.\n\n${sourceText}")
LANG_NAMES = {"zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese",
              "en": "English", "ja": "Japanese", "ko": "Korean", "fr": "French",
              "de": "German", "es": "Spanish", "ru": "Russian"}


def _load_llm_cfg() -> dict:
    cfg = {"endpoint": "", "key": "", "model": "", "prompt": DEFAULT_LLM_PROMPT,
           "temperature": 0.3, "timeout": 30.0, "thinking": "off"}
    path = os.environ.get("GTRANS_CONFIG",
                          os.path.expanduser("~/.config/ghostty-translate/llm.json"))
    try:
        with open(path) as f:
            for k, v in json.load(f).items():
                if k in cfg and v not in (None, ""):
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"llm config {path}: {e}", flush=True)
    for k, env in (("endpoint", "GTRANS_LLM_ENDPOINT"), ("key", "GTRANS_LLM_KEY"),
                   ("model", "GTRANS_LLM_MODEL"), ("prompt", "GTRANS_LLM_PROMPT"),
                   ("thinking", "GTRANS_LLM_THINKING")):
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    for k, env, cast in (("temperature", "GTRANS_LLM_TEMP", float),
                         ("timeout", "GTRANS_LLM_TIMEOUT", float)):
        if os.environ.get(env):
            cfg[k] = cast(os.environ[env])
    return cfg


LLM = _load_llm_cfg()

POLL_MS = 350          # selection poll interval
STABLE_READS = 2       # identical consecutive reads => selection finished
RERESELECT_S = 30      # re-show popup if the same text is selected again after this

_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK.findall(text)) / max(len(text), 1)


def pick_langs(text: str) -> tuple[str, str]:
    """Return (source, target): CJK text -> English, anything else -> TARGET_ZH."""
    if cjk_ratio(text) >= 0.3:
        return "zh-CN", TARGET_EN if TARGET_EN != TARGET_ZH else "en"
    return "auto", TARGET_ZH


def _http_json(url: str, data=None, timeout: int = 8, extra_headers=None):
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ghostty-translate"}
    headers.update(extra_headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def b_mymemory(text: str, src: str, tgt: str) -> str:
    if src == "auto":
        src = "en"  # MyMemory needs a concrete source; non-en text falls through to libre
    params = {"q": text[:900], "langpair": f"{src}|{tgt}"}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL
    d = _http_json("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params))
    if str(d.get("responseStatus")) != "200" or d.get("quotaFinished"):
        raise RuntimeError(f"mymemory refused: {d.get('responseDetails') or d.get('responseStatus')}")
    out = (d.get("responseData") or {}).get("translatedText", "")
    if not out or out.upper().startswith("MYMEMORY WARNING"):
        raise RuntimeError("mymemory: no translation")
    return out


def b_libre(text: str, src: str, tgt: str) -> str:
    if tgt == "zh-CN":
        tgt = "zh"
    d = _http_json(LIBRE_URL, {"q": text, "source": src, "target": tgt, "format": "text"})
    out = d.get("translatedText")
    if not out:
        raise RuntimeError(f"libre: {d}")
    if isinstance(out, list):  # some mirrors wrap in a list
        out = out[0].get("translatedText", "") if out and isinstance(out[0], dict) else str(out)
    return str(out)


def b_google(text: str, src: str, tgt: str) -> str:
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl="
           + urllib.parse.quote(src) + "&tl=" + urllib.parse.quote(tgt)
           + "&dt=t&q=" + urllib.parse.quote(text))
    d = _http_json(url)
    if not d or not d[0]:
        raise RuntimeError("google: empty")
    return "".join(seg[0] for seg in d[0] if seg and seg[0])


TRANSLATORS = {"mymemory": b_mymemory, "libre": b_libre, "google": b_google}


def _lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def _thinking_params(endpoint: str, level: str) -> dict:
    """Provider-specific chain-of-thought controls, mirroring
    zotero-pdf-translate's src/utils/thinkingParams.ts. Nothing is sent for
    unknown providers (strict APIs reject unknown fields)."""
    if level not in ("off", "low", "medium", "high"):
        return {}
    u = str(endpoint).lower()
    if "deepseek" in u:
        if level == "off":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"},
                "reasoning_effort": {"low": "low", "medium": "high", "high": "max"}[level]}
    if "openrouter" in u:
        return ({"reasoning": {"enabled": False}} if level == "off"
                else {"reasoning": {"effort": level}})
    if "openai" in u:
        return {"reasoning_effort": "minimal"} if level == "off" else {"reasoning_effort": level}
    if "dashscope" in u or "aliyun" in u:
        return {"enable_thinking": False} if level == "off" else {"enable_thinking": True}
    if "anthropic" in u or "claude" in u:
        return {}
    return {}


def b_llm(text: str, src: str, tgt: str) -> str:
    """OpenAI-compatible chat completion, mirroring zotero-pdf-translate's
    Custom GPT prompt format (${langFrom} / ${langTo} / ${sourceText})."""
    if not LLM["endpoint"] or not LLM["model"]:
        raise RuntimeError("llm not configured (set GTRANS_LLM_ENDPOINT/MODEL)"
                           if not LLM["endpoint"] else "llm model missing")
    url = str(LLM["endpoint"]).rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    prompt = (str(LLM["prompt"])
              .replace("${langFrom}", _lang_name(src) if src != "auto" else "the original language")
              .replace("${langTo}", _lang_name(tgt))
              .replace("${sourceText}", text))
    headers = {"Authorization": f"Bearer {LLM['key']}"} if LLM["key"] else {}
    body = {"model": LLM["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(LLM["temperature"]),
            "stream": False}
    body.update(_thinking_params(url, str(LLM.get("thinking", "off"))))
    d = _http_json(url, body, timeout=LLM["timeout"], extra_headers=headers)
    try:
        out = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"llm bad response: {str(d)[:200]}")
    out = (out or "").strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'“”「」" and not text.startswith(out[0]):
        out = out[1:-1].strip()
    if not out:
        raise RuntimeError("llm: empty translation")
    return out


TRANSLATORS["llm"] = b_llm



def translate(text: str) -> tuple[str, str, str]:
    """Return (translation, backend, 'src -> tgt'). Raises if all backends fail."""
    src, tgt = pick_langs(text)
    errors = []
    for name in BACKENDS:
        fn = TRANSLATORS.get(name)
        if not fn:
            continue
        try:
            return fn(text, src, tgt), name, f"{src} -> {tgt}"
        except Exception as e:  # noqa: BLE001 - fall through to next backend
            errors.append(f"{name}: {e}")
    raise RuntimeError("all backends failed: " + "; ".join(errors))


# ---------------------------------------------------------------- daemon ----

_X11 = {"display": os.environ.get("DISPLAY", ""), "auth": os.environ.get("XAUTHORITY", ""),
        "probed_at": 0.0}


def _probe_display() -> bool:
    """Find a reachable X display. Mutter restarts Xwayland on-demand with a new
    display number and auth file, so neither DISPLAY nor XAUTHORITY is stable."""
    import glob
    uid = os.getuid()
    auths = []
    if _X11["auth"]:
        auths.append(_X11["auth"])
    auths += sorted(glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*"),
                    key=os.path.getmtime, reverse=True)
    cands = [_X11["display"]] + [f":{p.rsplit('X', 1)[-1]}"
                                 for p in sorted(glob.glob("/tmp/.X11-unix/X*"))]
    seen = set()
    for d in [c for c in cands if c and not (c in seen or seen.add(c))]:
        for a in auths:
            env = {**os.environ, "DISPLAY": d, "XAUTHORITY": a}
            try:
                r = subprocess.run(["xdotool", "-d", d, "getdisplaygeometry"],
                                   env=env, capture_output=True, timeout=1)
                if r.returncode == 0:
                    _X11.update(display=d, auth=a)
                    return True
            except Exception:
                continue
    return False


def run_x(cmd: list[str], timeout: float = 2.0):
    """Run an X client with the current display env; re-probe on failure."""
    env = {**os.environ, "DISPLAY": _X11["display"], "XAUTHORITY": _X11["auth"]}
    r = None
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except Exception:
        r = None
    if r is None or r.returncode != 0:
        now = time.monotonic()
        if now - _X11["probed_at"] > 3:
            _X11["probed_at"] = now
            _probe_display()
        return None
    return r


def x11_env() -> dict:
    """Env for X clients; DISPLAY removed entirely when no X display is usable,
    so the popup falls back to a native Wayland window instead of dying.
    Probing is rate-limited: a zombie X socket is probed at most once a minute."""
    now = time.monotonic()
    if now >= _X11.get("no_x_until", 0.0):
        if _X11["display"] and run_x(["xdotool", "getdisplaygeometry"], timeout=1):
            env = dict(os.environ)
            env.update(DISPLAY=_X11["display"], XAUTHORITY=_X11["auth"])
            return env
        _probe_display()
        if _X11["display"] and run_x(["xdotool", "getdisplaygeometry"], timeout=1):
            env = dict(os.environ)
            env.update(DISPLAY=_X11["display"], XAUTHORITY=_X11["auth"])
            return env
        _X11["no_x_until"] = now + 60
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("XAUTHORITY", None)
    return env


def read_primary() -> tuple[str, bool]:
    """Return (text, x_alive). text="" with alive=True means no selection owner;
    alive=False means the X display is unreachable/hung."""
    if not _X11["display"]:
        return "", False
    env = {**os.environ, "DISPLAY": _X11["display"], "XAUTHORITY": _X11["auth"]}
    try:
        r = subprocess.run(["xclip", "-o", "-selection", "primary"], env=env,
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return "", False
    if r.returncode != 0:
        return "", "Can't open display" not in (r.stderr or "")
    return r.stdout[:8000].strip("\n").rstrip(), True


def read_clipboard() -> str:
    if not _X11["display"]:
        return ""
    env = {**os.environ, "DISPLAY": _X11["display"], "XAUTHORITY": _X11["auth"]}
    try:
        r = subprocess.run(["xclip", "-o", "-selection", "clipboard"], env=env,
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return ""
    return r.stdout[:8000].strip("\n").rstrip() if r.returncode == 0 else ""


def mouse_xy() -> tuple[int, int]:
    r = run_x(["xdotool", "getmouselocation", "--shell"])
    if not r:
        return -1, -1
    try:
        vals = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        return int(vals.get("X", -1)), int(vals.get("Y", -1))
    except Exception:
        return -1, -1


def _sock_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"ghostty-translate-{os.getuid()}.sock")


def _popup_send(payload: dict) -> bool:
    """One JSON message per connection to the popup server."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect(_sock_path())
        s.sendall(json.dumps(payload).encode() + b"\n")
        return True
    except Exception:
        return False
    finally:
        s.close()


def _start_popup_server(child_holder: dict) -> bool:
    p = child_holder.get("proc")
    if p is None or p.poll() is not None:
        env = x11_env()
        if env.get("DISPLAY"):  # X11 popup => we can position it at the mouse
            env["GDK_BACKEND"] = "x11"
        child_holder["proc"] = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--popup", "--serve",
             "--ttl", str(TTL)],
            env=env, start_new_session=True)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if _popup_send({"noop": 1}):
            return True
        # a dead server leaves a stale socket behind; it is replaced on spawn
        time.sleep(0.05)
    return False


def show_popup(orig: str, trans: str, child_holder: dict,
               ax: int = -1, ay: int = -1) -> None:
    """Update the single persistent popup window (never spawns per translation,
    so no flicker and no window pile-up). ax/ay anchor it to the selection."""
    if POPUP_CMD:
        cmd = POPUP_CMD.replace("{}", json.dumps({"orig": orig, "trans": trans}))
        subprocess.Popen(["sh", "-c", cmd], start_new_session=True)
        return
    payload = {"orig": orig, "trans": trans, "ax": ax, "ay": ay}
    for _ in range(2):
        if _popup_send(payload):
            return
        _start_popup_server(child_holder)
    notify(orig, trans)


def notify(orig: str, trans: str) -> None:
    try:
        subprocess.run(["notify-send", "-t", str(TTL * 1000), "-a", "ghostty-translate",
                        orig[:120].replace("\n", " "), trans[:400].replace("\n", " ")],
                       timeout=3)
    except Exception:
        pass


def daemon() -> None:
    llm_info = (f" | llm={LLM['model']} @ {LLM['endpoint']}"
                if LLM["endpoint"] and LLM["model"] else " | llm=off")
    print(f"ghostty-translate: watching PRIMARY selection "
          f"(zh<->{TARGET_EN}, backends={BACKENDS}){llm_info}", flush=True)
    child_holder: dict = {}
    seen: dict[str, float] = {}
    state = {"candidate": "", "hits": 0, "ax": -1, "ay": -1}
    own_texts: dict[str, float] = {}   # our own popup translations: never re-translate
    clip: dict = {"text": "", "ts": 0.0}   # system clipboard content + change time
    skips = {"n": 0}
    busy = threading.Lock()

    def worker(text: str, ax: int = -1, ay: int = -1) -> None:
        try:
            show_popup(text, "…", child_holder, ax, ay)   # instant feedback
            trans, backend, langs = translate(text)
            print(f"[{backend}] {langs}\n  {text[:80]!r}\n  => {trans[:80]!r}", flush=True)
            show_popup(text, trans, child_holder, ax, ay)
            skips["n"] = 0
            seen[text] = time.monotonic()
            own_texts[trans] = time.monotonic()
            for d, limit in ((seen, 128), (own_texts, 128)):
                if len(d) > limit:
                    for k, _ts in sorted(d.items(), key=lambda kv: kv[1])[:64]:
                        del d[k]
        except Exception as e:  # noqa: BLE001
            print(f"translate failed: {e}", flush=True)
            show_popup(text, f"（翻译失败：{str(e)[:80]}）", child_holder, ax, ay)
        finally:
            busy.release()

    def consider(cur: str) -> None:
        if cur == state["candidate"]:
            state["hits"] += 1
        else:
            # capture the anchor at FIRST sight of new selection text: that is
            # within one poll (~350 ms) of mouse-up, before the pointer wanders
            if cur:
                ax, ay = mouse_xy()
                if ax >= 0:
                    state["ax"], state["ay"] = ax, ay
                if os.environ.get("GTRANS_DEBUG"):
                    print(f"anchor: new-text {cur[:24]!r} mouse=({ax},{ay})",
                          file=sys.stderr, flush=True)
            state["candidate"], state["hits"] = cur, 1
            return
        text = state["candidate"]
        if state["hits"] < STABLE_READS or not MIN_CHARS <= len(text) <= MAX_CHARS:
            return
        if not _LETTER.search(text):
            return
        if time.monotonic() - seen.get(text, 0) < RERESELECT_S:
            return
        if time.monotonic() - own_texts.get(text, 0) < 300:
            return  # selecting our own translation (e.g. inside the popup) is a no-op
        if FILTER_MODE == "ghostty":
            # Ghostty (copy-on-select=clipboard) writes PRIMARY *and* CLIPBOARD
            # together; other apps' drag-selects only touch PRIMARY.
            if (clip["text"].strip() != text.strip()
                    or time.monotonic() - clip["ts"] > 3.0):
                skips["n"] += 1
                if os.environ.get("GTRANS_DEBUG"):
                    print(f"filter: skipping non-ghostty selection {text[:30]!r}",
                          file=sys.stderr, flush=True)
                elif skips["n"] % 20 == 0:
                    print("filter: 20 selections skipped (not Ghostty's?). If they "
                          "were Ghostty selections, add `copy-on-select = clipboard` "
                          "to ~/.config/ghostty/config and reload (ctrl+shift+,)",
                          flush=True)
                return
        if busy.acquire(blocking=False):
            threading.Thread(target=worker,
                             args=(text, state["ax"], state["ay"]),
                             daemon=True).start()

    def stop(*_a) -> None:
        p = child_holder.get("proc")
        if p and p.poll() is None:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def rss_guard() -> None:  # last-resort: never take the desktop down again
        import threading as _th
        def check():
            while True:
                time.sleep(5)
                try:
                    with open("/proc/self/status") as f:
                        rss = next(int(l.split()[1]) for l in f if l.startswith("VmRSS:"))
                    if rss > 300_000:  # kB
                        print(f"FATAL: RSS {rss} kB > 300 MB; exiting", flush=True)
                        os._exit(3)
                except Exception:
                    pass
        _th.Thread(target=check, daemon=True).start()

    rss_guard()

    # warm the popup server so the first selection is instant (window stays
    # hidden until the first update)
    threading.Thread(target=_start_popup_server, args=(child_holder,),
                     daemon=True).start()

    # Watcher priority is decided at runtime. In practice the X11 bridge (xclip
    # over Xwayland) is the reliable channel: GTK4's Wayland-native primary read
    # can report "empty clipboard" while a real selection exists, so it is only
    # a fallback. Both paths are bounded — one synchronous read per tick with a
    # hard timeout, consecutive failures switch/stop the watcher, and the RSS
    # guard above is the last resort.
    def xclip_watcher() -> None:
        fails, interval = 0, POLL_MS
        while True:
            cur, alive = read_primary()
            cb = read_clipboard()
            if cb != clip["text"]:
                clip["text"], clip["ts"] = cb, time.monotonic()
            if cur:
                fails, interval = 0, POLL_MS
                consider(cur)
            elif not alive:
                fails += 1
                interval = min(interval * 2, 2000)
                if fails >= 3:
                    print(f"x11 reads failing ({fails}x); switching watcher", flush=True)
                    return
            time.sleep(interval / 1000)

    def gdk_watcher() -> None:
        # Wayland-native GDK primary clipboard. Hardened after a real incident:
        # with a dead Xwayland selection bridge read_text_async may NEVER
        # complete, so reads are strictly single-flight, watchdogged, and the
        # watcher disables itself after repeated stalls instead of accumulating
        # operations (that once ate 24 GB).
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Gdk", "4.0")
            from gi.repository import GLib, Gtk, Gdk
            if not Gtk.init_check():
                raise RuntimeError("gtk init failed")
            disp = Gdk.Display.get_default()
            if disp is None:
                raise RuntimeError("no display")
            primary = disp.get_primary_clipboard()
        except Exception as e:  # noqa: BLE001
            print(f"GDK watcher unavailable: {e}", flush=True)
            time.sleep(30)
            return

        loop = GLib.MainLoop.new(None, False)
        st = {"pending": False, "t0": 0.0, "stuck": 0, "fails": 0, "interval": POLL_MS}

        def schedule():
            GLib.timeout_add(st["interval"], tick)

        def issue_read():
            if st["pending"]:
                return
            st["pending"], st["t0"] = True, time.monotonic()
            primary.read_text_async(None, on_read, None)

        def on_read(src, res, _):
            st["pending"] = False
            st["stuck"] = 0
            st["interval"] = POLL_MS
            try:
                consider((src.read_text_finish(res) or "")[:8000].strip("\n").rstrip())
            except Exception:  # empty clipboard etc.
                st["fails"] += 1

        def on_changed(*_a):
            st["fails"] = 0
            GLib.timeout_add(500, lambda: issue_read() or False)  # debounce drag

        def tick():
            if st["pending"]:
                if time.monotonic() - st["t0"] > 3.0:
                    st["pending"] = False  # abandon; never let reads pile up
                    st["stuck"] += 1
                    print(f"gdk read stalled ({st['stuck']}/3)", flush=True)
                    if st["stuck"] >= 3:
                        print("GDK watcher unreliable; stopping it", flush=True)
                        loop.quit()
                        return GLib.SOURCE_REMOVE
                    st["interval"] = min(st["interval"] * 4, 10000)
                return GLib.SOURCE_CONTINUE
            if st["fails"] > 40:
                st["interval"] = 2000
            issue_read()
            schedule()  # re-arm with the (possibly backed-off) interval
            return GLib.SOURCE_REMOVE

        try:
            primary.connect("changed", on_changed)
        except Exception:
            pass  # signal optional; polling covers it
        print("watcher: GDK primary clipboard (wayland-native, guarded)", flush=True)
        schedule()
        loop.run()

    while True:
        env = x11_env()
        if env.get("DISPLAY"):
            print("watcher: X11 primary via xclip (Xwayland bridge)", flush=True)
            xclip_watcher()
        else:
            print("watcher: no usable X; using GDK primary clipboard", flush=True)
            gdk_watcher()
        time.sleep(30)  # let the display environment settle before re-probing


# ----------------------------------------------------------------- popup ----

def run_popup_server(ttl: int) -> None:
    """Single persistent popup window. The daemon sends JSON updates over a Unix
    socket; the window is updated in place (no respawn => no flicker/pile-up)."""
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import GLib, Gtk
    except Exception as e:  # noqa: BLE001
        print(f"popup: GTK unavailable: {e}", file=sys.stderr)
        sys.exit(1)

    sockpath = _sock_path()
    app = Gtk.Application(application_id=f"org.ghostty.translate.p{os.getpid()}")

    def activate(_app):
        win = Gtk.Window(application=_app, decorated=False, resizable=False)
        win.set_title("Ghostty Translate")
        win.set_default_size(460, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(14); box.set_margin_bottom(14)
        box.set_margin_start(14); box.set_margin_end(14)
        lbl_o = Gtk.Label(label="", wrap=True, xalign=0, max_width_chars=54, selectable=False)
        lbl_o.set_opacity(0.55)
        lbl_t = Gtk.Label(label="", wrap=True, xalign=0, max_width_chars=54, selectable=True)
        box.append(lbl_o)
        box.append(Gtk.Separator())
        box.append(lbl_t)
        win.set_child(box)

        timers = {"hide": 0, "idle": 0}

        def _clear(tag):
            try:
                GLib.source_remove(tag)
            except Exception:
                pass

        def _xenv():
            return {**os.environ, "DISPLAY": _X11["display"],
                    "XAUTHORITY": _X11["auth"]}

        def _win_ids() -> list[str]:
            try:
                r = subprocess.run(["xdotool", "search", "--name",
                                    "^Ghostty Translate"],
                                   env=_xenv(), capture_output=True, text=True,
                                   timeout=1.5)
                return r.stdout.split()
            except Exception:
                return []

        def _raise() -> None:
            """Raise without touching input focus (XRaiseWindow, not activate)."""
            for wid in _win_ids():
                try:
                    subprocess.run(["xdotool", "windowraise", wid], env=_xenv(),
                                   capture_output=True, timeout=1.5)
                except Exception:
                    pass

        def _set_hints() -> None:
            """GTK4 dropped keep-above/type-hint; set the EWMH hints directly.
            NOTIFICATION type + ABOVE: mutter stacks it over normal windows
            (the terminal) and does not focus it."""
            for wid in _win_ids():
                for prop, fmt, value in (
                    ("_NET_WM_WINDOW_TYPE", "32a", "_NET_WM_WINDOW_TYPE_NOTIFICATION"),
                    ("_NET_WM_STATE", "32a",
                     "_NET_WM_STATE_ABOVE,_NET_WM_STATE_SKIP_TASKBAR,_NET_WM_STATE_SKIP_PAGER"),
                ):
                    try:
                        subprocess.run(["xprop", "-id", wid, "-f", prop, fmt,
                                        "-set", prop, value],
                                       env=_xenv(), capture_output=True, timeout=1.5)
                    except Exception:
                        pass

        def _move_to(px: int, py: int) -> bool:
            try:
                found = subprocess.run(["xdotool", "search", "--name",
                                        "^Ghostty Translate"],
                                       env=_xenv(), capture_output=True, text=True,
                                       timeout=1.5)
                for wid in found.stdout.split():
                    subprocess.run(["xdotool", "windowmove", "--sync", wid,
                                    str(px), str(py)],
                                   env=_xenv(), capture_output=True, timeout=1.5)
                return True
            except Exception:
                return False

        def _set_opacity(a: float) -> None:
            """GTK4's set_opacity is unreliable on XWayland; also set the EWMH
            property directly so hide/show always takes effect."""
            try:
                win.set_opacity(a)
            except Exception:
                pass
            val = "0" if a <= 0.0 else hex(int(max(0.0, min(a, 1.0)) * 0xFFFFFFFF))
            for wid in _win_ids():
                try:
                    subprocess.run(["xprop", "-id", wid, "-f", "_NET_WM_WINDOW_OPACITY",
                                    "32c", "-set", "_NET_WM_WINDOW_OPACITY", val],
                                   env=_xenv(), capture_output=True, timeout=1.5)
                except Exception:
                    pass

        def _set_input(full: bool) -> None:
            """Empty input region when hidden: the parked, transparent window
            must never swallow clicks."""
            try:
                import cairo
                s = win.get_surface()
                if s is not None:
                    s.set_input_region(None if full else cairo.Region())
            except Exception:
                pass

        def hide(*_a):
            timers["hide"] = 0
            # Unmap: a hidden window must be incapable of intercepting input.
            # (Parking it transparent at 0,0 still shadowed fullscreen windows
            # on the second monitor.)
            win.set_visible(False)
            return False

        def idle_quit(*_a):
            timers["idle"] = 0
            app.quit()
            return False

        def arm_hide():
            if timers["hide"]:
                _clear(timers["hide"])
            timers["hide"] = GLib.timeout_add_seconds(ttl, hide)

        def arm_idle():
            if timers["idle"]:
                _clear(timers["idle"])
            timers["idle"] = GLib.timeout_add_seconds(1800, idle_quit)

        place = {"seq": 0}
        anchor = {"x": -1, "y": -1}

        def _monitor_rect(x: int, y: int) -> tuple[int, int, int, int]:
            """Rect (x, y, w, h) of the monitor containing the point; WMs relocate
            a window that would cross monitor boundaries, so clamp inside it.
            Falls back to the nearest monitor (pointer mid-transition)."""
            rects = []
            try:
                r = subprocess.run(["xrandr", "--query"], env=_xenv(),
                                   capture_output=True, text=True, timeout=2)
                for line in r.stdout.splitlines():
                    m = re.search(r" connected(?: primary)? (\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                    if not m:
                        continue
                    w, h, mx, my = (int(v) for v in m.groups())
                    if mx <= x < mx + w and my <= y < my + h:
                        return mx, my, w, h
                    rects.append((mx, my, w, h))
            except Exception:
                pass
            if rects:  # nearest monitor (Chebyshev distance to its rect)
                return min(rects, key=lambda rc: max(
                    max(rc[0] - x, 0, x - (rc[0] + rc[2])),
                    max(rc[1] - y, 0, y - (rc[1] + rc[3]))))
            return 0, 0, 1920, 1080

        def _win_size() -> tuple[int, int]:
            try:
                g = subprocess.run(["xdotool", "search", "--name",
                                    "^Ghostty Translate",
                                    "getwindowgeometry", "--shell"],
                                   env=_xenv(), capture_output=True, text=True,
                                   timeout=1.5)
                vals = dict(l.split("=", 1) for l in g.stdout.splitlines() if "=" in l)
                return int(vals.get("WIDTH", 920)), int(vals.get("HEIGHT", 200))
            except Exception:
                return 920, 200

        def _primary_monitor_rect() -> tuple[int, int, int, int]:
            try:
                r = subprocess.run(["xrandr", "--query"], env=_xenv(),
                                   capture_output=True, text=True, timeout=2)
                for line in r.stdout.splitlines():
                    if " connected primary " in line:
                        m = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                        if m:
                            w, h, mx, my = (int(v) for v in m.groups())
                            return mx, my, w, h
                m = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
                             next(l for l in r.stdout.splitlines() if " connected" in l))
                w, h, mx, my = (int(v) for v in m.groups())
                return mx, my, w, h
            except Exception:
                return 0, 0, 1920, 1080

        def _place() -> bool:
            """One move attempt; True when the window landed where computed."""
            pw, ph = _win_size()
            if POSITION_MODE in ("bottom-center", "top-center"):
                # altgo overlay style: primary monitor, horizontally centered,
                # pinned to the bottom/top edge, EDGE_OFFSET = 80 logical px
                mx, my, mw, mh = _primary_monitor_rect()
                scale = max(1, round(pw / 460))     # the window is 460 logical wide
                offset = round(80 * scale)
                px = mx + (mw - pw) // 2
                py = (my + mh - ph - offset if POSITION_MODE == "bottom-center"
                      else my + offset)
                if os.environ.get("GTRANS_DEBUG"):
                    print(f"place[{POSITION_MODE}]: mon=({mx},{my},{mw},{mh}) "
                          f"size=({pw}x{ph}) -> ({px},{py})",
                          file=sys.stderr, flush=True)
            else:
                if anchor["x"] >= 0:
                    x, y = anchor["x"], anchor["y"]   # captured at selection time
                else:
                    x, y = mouse_xy()
                if x < 0:
                    return False
                mx, my, mw, mh = _monitor_rect(x, y)
                margin = 12
                # right/below the selection; flip left/above when there is no room
                px = x + 14 if x + 14 + pw + margin <= mx + mw else x - pw - 14
                py = y + 18 if y + 18 + ph + margin <= my + mh else y - ph - 18
                # stay inside the pointer's monitor (WMs relocate cross-screen windows)
                px = max(mx + margin, min(px, mx + mw - pw - margin))
                py = max(my + margin, min(py, my + mh - ph - margin))
                if os.environ.get("GTRANS_DEBUG"):
                    print(f"place[anchor]: anchor=({x},{y}) size=({pw}x{ph}) "
                          f"mon=({mx},{my},{mw},{mh}) -> ({px},{py})",
                          file=sys.stderr, flush=True)
            if not _move_to(px, py):
                return False
            try:
                g = subprocess.run(["xdotool", "search", "--name",
                                    "^Ghostty Translate",
                                    "getwindowgeometry", "--shell"],
                                   env=_xenv(), capture_output=True, text=True,
                                   timeout=1.5)
                vals = dict(l.split("=", 1) for l in g.stdout.splitlines() if "=" in l)
                return (abs(int(vals.get("X", px)) - px) <= 2
                        and abs(int(vals.get("Y", py)) - py) <= 2)
            except Exception:
                return False

        def _place_step(seq: int):
            def step():
                if place["seq"] != seq:
                    return False  # superseded by a newer update
                if _place():
                    place["seq"] += 1  # landed; cancel the remaining retries
                return False
            return step

        def position():
            place["seq"] += 1
            seq = place["seq"]
            for delay in (0, 200, 600):
                GLib.timeout_add(delay, _place_step(seq))
            return False

        def apply(payload: dict) -> None:
            orig = str(payload.get("orig", ""))[:2000]
            trans = str(payload.get("trans", ""))[:4000]
            if not trans:
                return  # noop/ping
            ax, ay = payload.get("ax"), payload.get("ay")
            if isinstance(ax, int) and isinstance(ay, int) and ax >= 0 and ay >= 0:
                anchor["x"], anchor["y"] = ax, ay
            else:
                anchor["x"] = anchor["y"] = -1
            lbl_o.set_text(orig)
            lbl_t.set_text(trans)
            win.set_title("Ghostty Translate · " + trans[:60].replace("\n", " "))
            _set_hints()        # type/state must be in place before the map
            _set_input(False)   # click-through: never swallow clicks
            _set_opacity(1.0)
            if not win.get_visible():
                win.present()   # remap; placement retries absorb map animation
            GLib.timeout_add(150, lambda: (_set_hints(), _raise(), False)[2])
            GLib.timeout_add(500, lambda: (_set_hints(), _raise(), False)[2])
            _raise()
            position()
            arm_hide()
            arm_idle()

        def handle_conn(fd, cond):
            conn = conns.get(fd)
            if conn is None:
                return False
            try:
                chunk = conn.recv(65536)
            except Exception:
                chunk = b""
            if not chunk:  # client closed (one message per connection)
                buf = bufs.pop(fd, b"")
                conns.pop(fd, None)
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    payload = json.loads(buf.decode() or "{}")
                    if isinstance(payload, dict):
                        apply(payload)
                except Exception:
                    pass
                return False
            bufs[fd] = bufs.get(fd, b"") + chunk
            return True

        conns: dict = {}
        bufs: dict = {}

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(sockpath)  # replace any stale socket
        except FileNotFoundError:
            pass
        srv.bind(sockpath)
        os.chmod(sockpath, 0o600)
        srv.listen(8)
        srv.setblocking(False)

        def on_accept(fd, cond):
            try:
                conn, _ = srv.accept()
            except BlockingIOError:
                return True
            except Exception:
                return True
            conn.setblocking(False)
            cfd = conn.fileno()
            conns[cfd] = conn
            GLib.io_add_watch(cfd, GLib.IO_IN | GLib.IO_HUP, handle_conn)
            return True

        GLib.io_add_watch(srv.fileno(), GLib.IO_IN, on_accept)

        def on_close(*_a):
            try:
                os.unlink(sockpath)
            except Exception:
                pass

        app.connect("shutdown", on_close)

        # click / key just hides; the server stays for the next selection
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: hide())
        win.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda *_: (hide(), False)[1])
        win.add_controller(keys)

        arm_idle()

        # Realize only (no map): the window stays unmapped until the first
        # translation, so it can never shadow other windows while idle.
        win.realize()
        GLib.timeout_add(100, lambda: (_set_hints(), False)[1])

        def _mem_guard():
            import threading as _th
            def check():
                while True:
                    time.sleep(10)
                    try:
                        with open("/proc/self/status") as f:
                            rss = next(int(l.split()[1]) for l in f
                                       if l.startswith("VmRSS:"))
                        if rss > 600_000:  # kB; GTK baseline is ~180 MB
                            print(f"popup FATAL: RSS {rss} kB > 600 MB", file=sys.stderr)
                            os._exit(4)
                    except Exception:
                        pass
            _th.Thread(target=check, daemon=True).start()

        _mem_guard()

    app.connect("activate", activate)
    app.run([])


# ------------------------------------------------------------------ main ----

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        text = " ".join(sys.argv[2:]).strip()
        if not text:
            sys.exit("usage: ghostty-translate.py --once TEXT")
        trans, backend, langs = translate(text)
        print(trans)
        print(f"[{backend} {langs}]", file=sys.stderr)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--popup":
        args = sys.argv[2:]
        ttl = int(args[args.index("--ttl") + 1]) if "--ttl" in args else TTL
        run_popup_server(ttl)
        return
    daemon()


if __name__ == "__main__":
    main()
