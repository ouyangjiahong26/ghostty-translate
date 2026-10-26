# ghostty-translate

Drag-select-to-translate for [Ghostty](https://ghostty.org) (works with any
terminal, actually) — behaves like zotero-pdf-translate: select text with the
mouse, a small popup appears near the cursor with the translation.

```
┌────────────────────────────────────────────┐
│ Consistency is the last refuge of the...   │  ← original (dimmed)
│ ────────────────────────────────────────── │
│ 缺乏想象力者的最后庇护所就是墨守成规。        │  ← translation (selectable)
└────────────────────────────────────────────┘
```

## Why not a Ghostty plugin?

Ghostty 1.3.x has no user plugin/extension API — its WASM runtime is for
embedding libghostty's VT engine, not for plugins (no plugin config keys, no
plugin CLI actions as of 1.3.1). This tool instead watches the terminal's
PRIMARY selection, which Ghostty fills on every mouse drag-select, so it needs
zero coupling to Ghostty.

## How it works

1. Daemon watches the PRIMARY selection (which Ghostty fills on every mouse
   drag-select). Preferred channel is the X11 bridge (`xclip` over Xwayland):
   in practice it is the only reliable one — GTK4's Wayland-native primary read
   can return "empty clipboard" while a real selection exists, so the native
   GDK path is only a fallback (hardened: single-flight reads, stall watchdog).
2. When the selection is stable (~0.7 s, i.e. mouse released) and not a
   duplicate, it translates:
   non-CJK → `zh-CN`, CJK → `en` (auto two-way, like zotero-pdf-translate).
3. Backends, in order: LLM (if configured) → MyMemory → LibreTranslate
   (disroot) → Google gtx. First success wins.
4. The popup is placed like altgo's overlay by default (`GTRANS_POSITION`):
   horizontally centered on the **primary monitor**, pinned near the bottom
   edge with an 80-logical-px offset (`bottom-center`, the default; also
   `top-center`). Set `GTRANS_POSITION=anchor` to instead follow the selection
   (right/below the anchor, flipping near screen edges, clamped to the
   selection's monitor). The daemon talks to the popup over a Unix socket, so
   updates replace the text in place: no flicker, no window pile-up. The popup
   is click-through and auto-hides after the TTL by being **unmapped**, so it
   can never intercept clicks — not even over fullscreen apps on another
   monitor.
5. Text equal to a recent translation (within 5 min) is ignored, so selecting
   inside the popup never triggers a feedback loop.
6. Only selections made **inside Ghostty** are translated (`GTRANS_FILTER`,
   default `ghostty`). Wayland never tells clients which app owns a selection,
   so Ghostty is fingerprinted by its dual write: with
   `copy-on-select = clipboard` it fills PRIMARY *and* CLIPBOARD together,
   while drag-select in other apps only touches PRIMARY. Requires that line in
   `~/.config/ghostty/config` (already added here); after changing it, reload
   Ghostty's config (`ctrl+shift+,`) or restart Ghostty. Set
   `GTRANS_FILTER=none` to translate selections from any application.

## Requirements

- python3
- `xclip` — selection watcher (required for the reliable X11-bridge path)
- PyGObject with GTK4 (`gir1.2-gtk-4.0` on Debian/Ubuntu) — popup UI + fallback watcher
- `xdotool` — popup placement at the mouse (optional but recommended)
- `notify-send` — fallback UI when the popup cannot be created

## Run

```sh
./ghostty-translate.py          # foreground
```

Autostart (systemd user unit):

```sh
cp ghostty-translate.service ~/.config/systemd/user/  # edit ExecStart to your clone path
systemctl --user daemon-reload
systemctl --user enable --now ghostty-translate
journalctl --user -u ghostty-translate -f   # see translations in the log
```

## Usage

- Drag-select text in Ghostty → popup ~1–2 s later. Re-selecting the same text
  within 30 s does not re-trigger.
- Inside vim/less/tmux mouse apps: hold **Shift** while dragging (Ghostty
  bypasses mouse reporting), or bind `toggle_mouse_reporting`.
- The popup translation label is selectable if you want to copy it.

## Control panel

```sh
ghostty-translate --gui      # or launch "Ghostty Translate" from the app menu
```

A small GTK4 window controls the tool live: translation switch, popup
placement, response scope, display duration, LLM model and thinking level,
service/last-translation status, a test-translation button and a log viewer.
Changes are written to `~/.config/ghostty-translate/settings.json` and
`llm.json`; the running daemon hot-reloads them (placement/TTL changes respawn
the popup window automatically).

Install the app-menu entry and CLI symlink:

```sh
ln -s "$PWD/ghostty-translate.py" ~/.local/bin/ghostty-translate
cp ghostty-translate.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications
```

## Runtime switch

```sh
./ghostty-translate.py off      # pause: no translations
./ghostty-translate.py on       # resume
./ghostty-translate.py toggle   # flip
./ghostty-translate.py status   # paused | active
```

The state lives in `~/.config/ghostty-translate/paused` (survives restarts),
and each toggle pops a short on-screen confirmation. For one-keystroke control,
bind a GNOME custom shortcut (Settings → Keyboard → Custom Shortcuts) to the
`toggle` command.

## Configuration (environment)

| Variable                 | Default                          | Meaning                              |
|--------------------------|----------------------------------|--------------------------------------|
| `GTRANS_PRIMARY_TARGET`  | `zh-CN`                          | target for non-CJK text              |
| `GTRANS_SECONDARY_TARGET`| `en`                             | target for CJK text                  |
| `GTRANS_TTL`             | `8`                              | popup auto-close seconds             |

| `GTRANS_MIN_CHARS`       | `2`                              | ignore shorter selections            |
| `GTRANS_MAX_CHARS`       | `1500`                           | ignore longer selections             |
| `GTRANS_BACKENDS`        | `mymemory,libre,google`          | backend order                        |
| `GTRANS_LIBRE_URL`       | `https://translate.disroot.org/translate` | LibreTranslate endpoint     |
| `GTRANS_EMAIL`           | empty                            | MyMemory email (raises free quota)   |
| `GTRANS_POPUP_CMD`       | empty                            | custom popup: `{}` → JSON payload    |
| `GTRANS_FILTER`          | `ghostty`                        | `ghostty` = only Ghostty selections (dual-write fingerprint), `none` = any app |
| `GTRANS_POSITION`        | `bottom-center`                  | `bottom-center`/`top-center` (altgo style, primary monitor), `anchor` (follow selection) |
| `GTRANS_LLM_ENDPOINT`    | from config file                 | OpenAI-compatible base URL or full `/chat/completions` |
| `GTRANS_LLM_KEY`         | from config file                 | API key (`Authorization: Bearer`)    |
| `GTRANS_LLM_MODEL`       | from config file                 | model name                           |
| `GTRANS_LLM_PROMPT`      | built-in                         | prompt with `${langFrom}` `${langTo}` `${sourceText}` |
| `GTRANS_LLM_TEMP`        | `0.3`                            | sampling temperature                 |
| `GTRANS_LLM_TIMEOUT`     | `30`                             | LLM request timeout (s)              |
| `GTRANS_CONFIG`          | `~/.config/ghostty-translate/llm.json` | LLM config file                |

## LLM translation (like zotero-pdf-translate's GPT service)

Order of resolution: config file, then environment variables override. The
backend is OpenAI-compatible (DeepSeek, OpenAI, SiliconFlow, Ollama, vLLM,
LM Studio, …). Any failure falls through to the next backend in
`GTRANS_BACKENDS`, so an LLM outage degrades to MyMemory instead of no
translation.

```json
{
  "endpoint": "https://api.deepseek.com/v1",
  "key": "sk-...",
  "model": "deepseek-chat",
  "prompt": "As an expert translator, translate from ${langFrom} to ${langTo}. Reply with only the translation.\n\n${sourceText}",
  "temperature": 0.3,
  "timeout": 30
}
```

The prompt format matches zotero-pdf-translate's Custom GPT
(`${langFrom}` / `${langTo}` / `${sourceText}`), so an existing Zotero prompt
can be pasted verbatim. This machine's `llm.json` was imported from the
Zotero plugin config (`extensions.zotero.ZoteroPDFTranslate.customGPT2.*`
plus the key from `secretObj`), so both tools translate identically.

### Latency & LLM thinking

The popup shows a `…` placeholder at the selection ~0.6 s after mouse-up; the
finished translation lands ~1.6 s later (DeepSeek, thinking disabled).
Chain-of-thought is **off** by default (`"thinking": "off"` in `llm.json`,
env `GTRANS_LLM_THINKING`), mirroring zotero-pdf-translate's thinkingLevel:
DeepSeek `{"thinking": {"type": "disabled"}}`, OpenRouter
`{"reasoning": {"enabled": false}}`, OpenAI `reasoning_effort: "minimal"`;
unknown providers get no extra parameter (strict APIs reject unknown fields).
Set `"thinking": "low"|"medium"|"high"` to re-enable.


For systemd, add `Environment=` lines to the unit, e.g.
`Environment=GTRANS_TTL=12`.

## Notes & limits

- GNOME recycles Xwayland on demand; when no usable X display exists the popup
  is a native Wayland window (compositor-chosen position, not at the mouse) —
  everything else still works.
- MyMemory anonymous quota is ~5 k chars/day per IP; set `GTRANS_EMAIL` or
  reorder backends if you hit it. Google gtx needs an IP Google likes.
- `--once "text"` translates once and prints (for testing backends).

## Safety

An early version once froze the whole desktop (24 GB leak in 7 min) by issuing
an unbounded stream of clipboard reads into a dead-Xwayland selection bridge.
The watcher is now hardened:

- reads are strictly single-flight (a new one is never issued while pending),
- a stalled read (>3 s) is abandoned; 3 stalls disable the watcher entirely,
- failing reads back the poll interval off to 2 s,
- the daemon self-exits if its RSS exceeds 300 MB (systemd then restarts it).

Steady state is ~60 MB RSS with zero pending reads.
