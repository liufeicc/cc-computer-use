# cc-computer-use — Computer-Use MCP Server

[中文文档](README.zh-CN.md) | **English**

An MCP server that lets an AGENT (Claude Code, etc.) drive a Linux desktop — **without taking over
yours**.

Most computer-use servers do the same thing: they seize your mouse, your keyboard and your screen for
the whole run. This one puts the agent on **its own private virtual screen** by default, so you keep
working while it does. When you *do* want it driving the real desktop, one environment variable
switches it over.

| | Conventional computer-use MCP | This project |
|---|---|---|
| While the agent works | Mouse / keyboard / screen are **seized** — you can't use your machine | **Untouched** — the agent gets a private Xephyr virtual screen |
| Isolating the agent | You build the VM or container yourself | **On by default**, zero setup |
| Sandbox unavailable | Silently falls back to your real desktop | **Refuses to run** and says why |
| The a11y tree | One AT-SPI bus shared with your desktop | **Private AT-SPI bus** — the host bus cannot see sandboxed apps |

> Anthropic's own computer-use reference says running such an agent outside a VM is
> *"strongly discouraged"* and that there are *"no safeguards"*. This server takes that seriously —
> isolation is the default here, not a setting you have to get right.

## How it perceives

Perception goes through the **accessibility tree (AT-SPI)** and action through **element-level
operations**, instead of the conventional "screenshot + coordinate click". That removes two more
problems at the root:

| Pain point | Conventional approach | This project |
|---|---|---|
| Screenshots burn tokens | 1000+ tokens per screenshot, every round | Read the a11y tree / OCR text — both compact text |
| Clicks miss the target | LLM estimates coordinates from pixels; DPI and multi-monitor stack up the error | Element-level `do_action`: **zero coordinates**, immune to focus / resolution / DPI |

> Feasibility was established by measurement in [`demo/`](demo/README.md) — the conclusion there was
> that **element-level actions beat coordinate clicking outright**, which is why the execution layer
> here is "element-level first, coordinate clicking only as fallback".
>
> For a full deployment walkthrough see [`docs/安装说明.md`](docs/安装说明.md) (Chinese).

---

## 1. Capabilities (17 MCP tools)

| Tool | Purpose |
|------|---------|
| `get_ui_tree` | Read the accessibility tree as compact text, each actionable element tagged with a `[ref]` — **the primary sense for apps that expose a tree** |
| `get_screen_text` | **OCR the screen into text plus coordinates** (`[ref] text @ (x,y)`) — **the primary sense for grey-area apps**, far cheaper than screenshots. Defaults to `scope="window"` (active window only): a dialog takes 0.3–3 s while a dense full screen takes 8–10 s |
| `find_element` | Search elements by text / role / app; returns candidates carrying `ref`s |
| `element_info` | Inspect one element: value, states, rect, available actions |
| `click` | **Three-tier fallback**: element-level `do_action` → calibrated coordinate click → screenshot fallback. In grey areas you can pass raw `x`+`y`. Coordinate clicks return **landing evidence**: which window received it, what text sits under the point, the active window afterwards, plus a **crosshair image** (red cross = the exact pixel clicked), a **computed aim offset** (`expect="Save"` → "missed, off by (+17,-42), 45px total — try its center (517,258)"), and a **post-click change percentage**. `preview=false` disables the visual feedback |
| `get_last_click_image` | **Look back** at the most recent coordinate click (`index=-1` latest, `-2` before that). When a click produces no visible reaction, inspect where it actually landed before blindly clicking again |
| `type_text` | Input: element-level `set_value` first, keyboard injection as fallback. Reports the active window after typing, so a stolen focus is immediately visible |
| `press_key` | Key combos (`ctrl+s`, `alt+F4`, …); reports the active window afterwards |
| `act_sequence` | Batch a run of actions in one call (click / type / key / wait / sleep / list_windows / **ui_tree** / **screenshot**) — the main round-trip saver. Putting `ui_tree` or `screenshot` last returns "what the screen looks like now" **inside the same call**. `click` also accepts a **list of candidate names** (`["Save","保存"]`): the first one that actually exists is clicked and the rest are never tried |
| `double_click` | **Double click** — coordinate-level only, because element-level actions have no double-click semantics, so you must pass screen coordinates. The two clicks are timed server-side (100 ms, far below the ~400 ms system threshold), so it genuinely *is* a double click; two separate `click` calls never are |
| `scroll` | Scroll at a point. `direction` is up/down; `amount` is in **detents** (one wheel event = one detent; there is no half detent). ⚠️ How far a detent scrolls is decided by the *app* (GTK ≈ 3 lines), so the pattern is scroll → look → scroll again — batch it with `ui_tree`/`screenshot` in one `act_sequence` |
| `drag` | Drag from `(from_x,from_y)` to `(to_x,to_y)`. Interpolates a run of intermediate moves — a single teleporting move is not recognised as a drag by many apps |
| `launch_app` | Launch an app on the target display (the proper way to get an app into the sandbox) |
| `list_windows` | Visible windows (id / title / PID / geometry, largest first) |
| `wait_window` | Wait until a window title matches (server-side polling, single call) |
| `get_screen_layout` | Monitor layout and virtual desktop size |
| `screenshot` | Screenshot — **last resort**, JPEG by default; use only when pixel-level judgement is genuinely needed |

### Recommended workflow

- **Apps with a tree**: `launch_app` → `get_ui_tree` to see the structure → locate the target
  `[ref]` (or `find_element`) → `click(ref)` / `type_text(ref)`.
- **Grey-area apps** (SWT/Java, custom-drawn widgets, games, remote desktops — no a11y tree):
  `get_screen_text` to obtain text and coordinates → `click(ref)` or `click(x=.., y=..)`.
  **Do not** default to screenshots and let the model guess coordinates: in a measured DBeaver
  session of 8.3 minutes, tool calls accounted for 21 seconds while the remaining 478 seconds went
  entirely into "look at image → estimate pixel → convert back to screen coordinates".
- **When unsure about a coordinate, pass `expect`**: `click(x=512, y=384, expect="Save")` makes the
  program compute how far off you were and which coordinate to use instead. If a click does nothing,
  call `get_last_click_image` before retrying — never repeat clicks blindly.
- **Batch any predictable sequence into a single `act_sequence`.** Every extra call costs a
  model-think + read-result round trip (30–70 s measured). Split calls only when you need to branch
  on the result. Unsure what the target is called? Pass a **list of candidate names**
  (`text: ["Save","保存"]`) — the server tries them in order and stops at the first one that exists,
  instead of spending one round trip per guess.

### Three mechanisms that save round trips

1. **Landing evidence** — the return value of a coordinate click or key injection only tells you the
   event was *sent* (xdotool has no idea what it hit). So clicks also report:

   ```
   coordinate click executed @(874,578) | landed on: window "Landing Story" 310x212
   | text under point: "OK" | active window after: none (previous "Landing Story" is gone)
   ```

   The model immediately knows it hit "OK" and the dialog really closed — **no confirming screenshot
   needed**. (Element-level `do_action` doesn't need this; there is no "did it hit" question.)

2. **The `ui_tree` and `screenshot` steps of `act_sequence`** — "open a menu → read what's in it"
   used to take two calls. Two thirds of the screenshots in the measured session existed purely to
   confirm a preceding sequence. Both now fold into the same call: **`ui_tree` for structure**
   (text, cheap), **`screenshot` for pixels** (expensive — grey-area apps with no tree have only
   this option). The image comes back as an extra image content block alongside the step log.

3. **The server nudges the model to batch.** The MCP `instructions` handed to the model at session
   start state the rule outright: one round trip costs 30–70 s while the action itself takes
   fractions of a second, so consecutive actions that don't depend on the previous result belong in
   one `act_sequence`. Without that nudge the model reads a step-by-step workflow and defaults to one
   call per action — which is exactly what the 96% round-trip overhead above consists of.

### Click feedback: the ring, the crosshair, the computed offset

Coordinate clicks exist **only** in tier ② (fallback) and raw-coordinate grey-area use, and all of the
feedback below is attached automatically by `backend.click_at`.

- **Ring** (`backend/linux/ring.py`) — for the **human** watching the sandbox: a solid red ring lights
  up at the landing point and self-destructs after 1.5 s. Without it, a click in the virtual screen is
  invisible to the user. Disable with `CC_CU_CLICK_RING=0`.
- **Crosshair preview image** — for the **model**: a 480x300 crop grabbed *before* the click with a red
  cross on the landing point, **unscaled** (1 px on the image = 1 px on screen, so no arithmetic is
  needed). ~190 tokens per call. `preview=false` skips the capture entirely (zero cost); disable
  globally with `CC_CU_CLICK_PREVIEW=0`.
- **Computed aim offset** — the landing coordinate is one the program itself sent (exact), and the text
  block positions come from OCR (which reports `left/top/width/height`). The offset is therefore pure
  arithmetic, not a guess. Passing `expect` turns a heuristic into an exact number.
- **Post-click change** — a before/after pixel comparison of the same region (<0.5 % none, <5 % slight,
  ≥5 % obvious). This is an independent second signal that needs no knowledge of intent. Read the two
  signals together: off by 45 px but the UI changed means the button's hit area is larger than its
  text; off by 45 px with no change means the coordinate needs fixing.

---

## 2. Requirements

| Item | Requirement |
|------|-------------|
| OS | Linux (Phase 1 is Linux only; Windows is Phase 3) |
| Session type | **X11** (under Wayland, global input injection is restricted) |
| Python | 3.12+ |
| System components | `xdotool` (injection), `xserver-xephyr` (sandbox virtual screen), `xclip` (clipboard path for non-ASCII input), `wmctrl` (app enumeration), `tesseract-ocr` + `chi_sim` (OCR grey-area sensing), `zenity` (tests), AT-SPI (`gir1.2-atspi-2.0` / `libatspi`) |
| Accessibility | Must be enabled (see below) |

> **Key constraint**: AT-SPI depends on the system's `libatspi` and the `Atspi` typelib — OS-level
> components that cannot be bundled into a single binary. `_bootstrap.py` points `GI_TYPELIB_PATH` at
> the system directory at runtime, so even as a frozen executable these components must still be
> provided by the host system.

---

## 3. Installation

```bash
# 1. One-time: enable the accessibility switch (otherwise GTK app trees are unreadable)
gsettings set org.gnome.desktop.interface toolkit-accessibility true

# 2. System dependencies (Debian/Ubuntu) — OS-level, not bundled into the build artifact
sudo apt install -y xdotool xserver-xephyr xclip wmctrl zenity \
                    gir1.2-atspi-2.0 \
                    tesseract-ocr tesseract-ocr-chi-sim

# 3. conda environment (Python >= 3.12); pygobject must come from conda-forge — pip cannot install gi
conda create -n cc-computer-use python=3.12 -y
conda install -n cc-computer-use -c conda-forge pygobject -y
conda activate cc-computer-use

# 4. Python dependencies + this project (editable)
PYTHONNOUSERSITE=1 pip install -U mcp cryptography pyinstaller pytest
PYTHONNOUSERSITE=1 pip install -e .
```

> **Why `PYTHONNOUSERSITE=1`**: if another `mcp` lives in `~/.local`, it shadows the conda
> environment's package and causes version confusion (affecting both development and packaging). All
> run/build commands should carry this prefix.
>
> **Note**: `chi_sim` is the Simplified Chinese OCR language pack; without it Chinese text comes out as
> garbage. If your UI is English-only, set `CC_CU_OCR_LANG=eng` — noticeably faster.
>
> **Moving to a new machine / deploying from scratch / troubleshooting**: see
> [`docs/安装说明.md`](docs/安装说明.md) (Chinese, step-by-step).

---

## 4. Running the MCP server (development mode)

```bash
# Self-check: prints backend status, tool list, screen layout (does not enter the stdio loop)
PYTHONNOUSERSITE=1 python -m computer_use_mcp.server --selftest

# Normal run (stdio transport, for MCP clients)
PYTHONNOUSERSITE=1 python -m computer_use_mcp.server
```

### Isolation sandbox (on by default)

X11 has a single physical pointer and focus: while the agent injects input, the user cannot use the
machine. So this server **defaults to a visible Xephyr virtual screen** (the display number is
allocated automatically, one per Claude session). All injection, screenshots and geometry queries
target that virtual screen — the host desktop is untouched and the user can keep working. You can
also reach into the Xephyr window with your own mouse and keyboard at any time; the agent's injection
politely yields until you leave.

The virtual screen starts **only when this MCP is first actually used**, not when Claude Code launches
— otherwise every session would spawn a stray Xephyr window.

**Each Claude session gets its own private screen** (allocated by the X server, race-free), so
concurrent sessions never fight over focus or the clipboard. Within one session, the main agent and
its sub-agents share that screen, and injection is therefore **mutually exclusive**: whoever fails to
acquire the screen lock receives an immediate "screen is busy, retry later" error rather than being
silently queued (queueing would execute decisions made against a stale view of the UI).

If the sandbox is unavailable (Xephyr missing, or startup failed), injection is **refused with an
explicit error** rather than silently falling back to your real desktop. To operate the real desktop
on purpose, set `CC_CU_DISPLAY_MODE=real`.

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `CC_CU_DISPLAY_MODE` | `isolated` | Set to `real` to disable the sandbox and operate the real desktop |
| `CC_CU_SANDBOX_DISPLAY` | *unset* | Unset = allocate a free display per session; set explicitly (e.g. `:99`) = fixed display, and attach to it rather than restarting if it already exists |
| `CC_CU_SANDBOX_SCREEN` | `1600x1000` | Virtual screen resolution |
| `CC_CU_SANDBOX_WAIT_USER` | `30` | Max seconds injection yields while the user is inside the sandbox |
| `CC_CU_SANDBOX_WM` | `auto` | Set to `none` to skip launching the i3 window manager inside the sandbox |
| `CC_CU_SANDBOX_AT_SPI_BUS` | *unset* | Override the AT-SPI bus address for sandboxed apps (debugging) |
| `CC_CU_OCR_LANG` | `chi_sim+eng` | OCR languages; `eng` for English-only UIs is noticeably faster |
| `CC_CU_CLICK_RING` | `1` | Set to `0` to disable the visual click ring |
| `CC_CU_CLICK_PREVIEW` | `1` | Set to `0` to disable the crosshair preview image globally |
| `PYTHONNOUSERSITE` | — | **Set to `1` for every command** so `~/.local` packages cannot shadow the conda env |

### a11y isolation: the sandbox has its **own** AT-SPI bus

**Xephyr isolates the X11 channel (injection, screenshots) but not AT-SPI.** Accessibility runs over
the session D-Bus, and the sandbox shares one `at-spi2-registryd` with the host. Measured on
2026-09-15: traversing the a11y tree of an app inside the sandbox **crashed the host GNOME Shell**.
Offline core analysis confirmed the root cause — gnome-shell's own atk-bridge calling `g_object_ref`
on an already-freed GObject (a use-after-free), crashing on its main loop thread.

**Solution**: when the sandbox starts it brings up a **private** AT-SPI bus (its own `dbus-daemon` +
`at-spi2-registryd` in a private directory). Sandboxed apps and the MCP's a11y reader both connect to
it — **the host bus cannot see any sandboxed app**, while a11y capability is fully preserved.

```
host GNOME Shell ── host AT-SPI bus ── host apps (gnome-shell / chrome / …)
                                    ✗ mutually invisible
sandboxed apps ──── private sandbox AT-SPI bus ── the MCP's a11y reader
```

- If the private bus fails to start, apps receive a **dead address** (no a11y connection): the host
  stays safe but no tree is readable inside the sandbox. This is deliberate — better to have no a11y
  than to let traffic reach the host bus.
- Why a bus address rather than `NO_AT_BRIDGE=1`: the latter is a GTK3-only switch (GTK4 uses
  `GTK_A11Y`), and SWT (Java apps such as DBeaver) honours **neither**. `AT_SPI_BUS_ADDRESS` is
  toolchain-agnostic — GTK3/GTK4/SWT/Qt/Electron all route a11y through libatspi, all read it, and
  none of them fall back to the session bus (measured).
- Verification script: `PYTHONNOUSERSITE=1 python tests/manual_a11y_isolation.py`

> **Operational consequence**: if the sandbox crashes and is rebuilt, the bus address changes, and
> libatspi's `atspi_init()` can only ever bind once per process — so a11y stays permanently
> unavailable for the rest of that session, leaving only OCR + coordinate clicking. Restarting Claude
> Code is the only recovery.

---

## 5. Building a standalone executable

```bash
bash build.sh
# Artifacts (onedir, not a single file):
#   dist/computer-use-mcp-bin/     directory holding the real executable, ~455 MB
#   dist/computer-use-mcp          thin shell wrapper forwarding to it

# Verify the frozen artifact
PYTHONNOUSERSITE=1 ./dist/computer-use-mcp --selftest
```

**Why onedir**: onefile re-extracts itself to a temp directory on every launch, making cold start an
order of magnitude slower. The wrapper exists to **preserve the registered path** (`~/.claude.json`
points at `dist/computer-use-mcp`) — do not delete it.

Key points baked into `build.sh`:
- **Targeted `gi` collection** (`--collect-submodules/--collect-data/--collect-binaries gi`) plus
  explicit `--exclude-module` for the GTK family: `--collect-all gi` drags in `Gtk`, which trips
  PyInstaller's GTK hook into collecting 185 MB of icons and 42 MB of themes — none of which this
  project ever imports.
- `--collect-submodules mcp.server` (**not** all of `mcp`, which would pull in `mcp.cli` and its
  `typer` dependency).
- `--copy-metadata`: mcp/pydantic and friends read versions via `importlib.metadata`.
- **`LD_LIBRARY_PATH` must prepend conda's `lib`**: otherwise PyInstaller pairs the system's old
  `libcrypto` with conda's new `libssl` and fails at runtime with `OPENSSL_3.3.0 not found`.
- **`packaging/embed-atspi.sh` bundles `libatspi.so.0` and the `Atspi-2.0.typelib`** (into
  `_internal/` and `_internal/gi_typelibs/`), so the artifact no longer needs the target machine to
  have `libatspi2.0-0` / `gir1.2-atspi-2.0`. Both locations are forced: the latter is the only place
  PyInstaller's `pyi_rth_gi` hook looks (it *assigns* `GI_TYPELIB_PATH`, not appends), and the former
  sits under `_MEIPASS` so the subprocess-env stripping logic removes it automatically. The script
  also **walks the typelib dependency closure** — `Atspi-2.0` depends on `DBus-1.0`, which comes from
  `gir1.2-freedesktop`; copying just the one file works fine on a dev box and fails on the target.
- `--collect-submodules Xlib` plus explicit `--hidden-import Xlib.ext.shape`: the click ring's X
  extension modules are imported dynamically by name, so they appear in no static import graph. Omit
  them and the ring silently works in development but **vanishes from the frozen build**.

**Distributing to another machine** requires the whole `dist/` directory; the target machine still
needs the system dependencies installed (`xdotool` / `xserver-xephyr` / `tesseract` / AT-SPI, etc.).

### Building a self-contained `.deb` (recommended for Ubuntu users)

Unlike `build.sh` above (which uses this machine's 24.04 base and therefore cannot be handed to a
22.04 user), this path builds inside a container pinned to the oldest supported base:

```bash
mkdir -p ~/dist
docker run --rm -v "$PWD":/src -v "$HOME/dist":/out \
  -v /tmp/Miniforge3-Linux-x86_64.sh:/miniforge.sh:ro \
  -e CC_CU_MINIFORGE_SH=/miniforge.sh \
  ubuntu:22.04 bash -c 'bash /src/packaging/build-in-container.sh'
# → ~/dist/cc-computer-use_0.1.0-1_amd64.deb  (68 MB measured)

# Accept it in a clean container (install → run → click → uninstall; run it on both 22.04 and 24.04)
bash packaging/deb/verify-install.sh ~/dist/*.deb ubuntu:22.04
```

On the target machine, `sudo apt install ./cc-computer-use_*.deb` is all it takes — **nothing else
needs installing**. The nine system binaries (xdotool / Xephyr / i3 / tesseract / …), their
dependency closure, the Atspi typelib and the OCR language data all ship inside. Afterwards
`cc-computer-use-setup` flips the accessibility switch and registers the MCP server if Claude Code
is present (existing config is never overwritten); `cc-computer-use-doctor` reports on every piece.

The build must run inside **ubuntu:22.04**: the binaries collected on 24.04 require GLIBC 2.38,
which would exclude 22.04 — the oldest supported base. See
[`docs/安装说明.md`](docs/安装说明.md) section 7 for the details.

---

## 6. Wiring it into Claude Code

Add one of the following to your project or `~/.claude` MCP configuration:

**A. Using the packaged executable (recommended, no conda needed)**
```json
{
  "mcpServers": {
    "cc-computer-use": {
      "type": "stdio",
      "command": "/absolute/path/cc-computer-use/dist/computer-use-mcp",
      "args": [],
      "env": { "PYTHONNOUSERSITE": "1" }
    }
  }
}
```

**B. Running from the conda environment (development; source edits take effect immediately)**
```json
{
  "mcpServers": {
    "cc-computer-use": {
      "type": "stdio",
      "command": "/path/to/conda/envs/cc-computer-use/bin/python",
      "args": ["-m", "computer_use_mcp.server"],
      "env": {
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/absolute/path/cc-computer-use/src"
      }
    }
  }
}
```

> ⚠️ **The server name must be `cc-computer-use`, not `computer-use`** — the latter fails to register
> in practice; renaming fixes it.
>
> Claude Code must be running inside an **X11 graphical session** (the server subprocess inherits
> `DISPLAY` so it can read the screen and inject input).

Once connected you can give natural-language tasks, for example:
- "What buttons are clickable in the current window?" → the agent calls `get_ui_tree`
- "Click 'Yes' in this dialog" → `find_element` + `click(ref)`, element-level, zero coordinates
- "Type hello in the editor and save" → `type_text` + `press_key('ctrl+s')`

---

## 7. Testing

```bash
# Unit tests (no desktop needed): geometry calibration / ref mapping / serializer denoising /
# injection and sandbox logic. 310 tests collected; without the e2e flag: 304 passed, 6 skipped
PYTHONNOUSERSITE=1 python -m pytest -q

# End-to-end (needs X11 + zenity + Xephyr): runs INSIDE the sandbox by default, never touching the
# real desktop; requires explicit opt-in. 310 passed
CC_CU_E2E=1 PYTHONNOUSERSITE=1 python -m pytest -q

# Manual stories (6 of them; full list in docs/安装说明.md §5.4)

# Sandbox story (drives a real server): launches zenity in the sandbox → element-level click →
# asserts the host was left completely undisturbed
PYTHONNOUSERSITE=1 python tests/manual_sandbox_story.py

# Grey-area + landing-evidence story (drives the frozen artifact; run `bash build.sh` first):
# OCR yields coordinates → click → asserts the dialog actually closed
PYTHONNOUSERSITE=1 python tests/manual_ocr_story.py
```

> ⚠️ While `manual_sandbox_story.py` runs, **do not touch your mouse or keyboard** — it asserts that
> the host's active window and pointer are unchanged.

The suite is split deliberately: unit tests never need a desktop, end-to-end tests require X11 plus
`zenity` and `Xephyr` (and skip automatically when unavailable), and `manual_*.py` files are manual
stories that drive a real server or the frozen artifact through complete task flows.

---

## 8. Architecture

```
Claude Code / MCP client
        │ MCP protocol (stdio, JSON-RPC)
┌───────▼──────────────────────────────────────────┐
│  server.py   (FastMCP/MCPServer entry)            │
│  tools/          17 tool definitions (schema +    │
│                  guiding descriptions)            │
│  core/coordinator   semantic orchestration: the   │
│                     three-tier fallback           │
│  core/serializer    tree → compact text (token    │
│                     saving) + ref allocation      │
│  core/geometry      coordinate calibration        │
│  core/display       isolation sandbox: the single │
│                     source of DISPLAY             │
│  utils/refs         ref ↔ live element mapping    │
│                     (in-process cache)            │
└───────┬──────────────────────────────────────────┘
        │ backend/base.py  abstract contract
┌───────▼──────────┐
│ backend/linux/   │  AT-SPI reads (atspi.py) + xdotool injection (inject/)
└──────────────────┘  (Windows backend = Phase 3)
```

**The three-tier fallback (`coordinator.click`)**:
1. **element**: `backend.invoke()` → AT-SPI `do_action`, zero coordinates (preferred).
2. **coord**: `geometry` calibrates an absolute screen coordinate → `xdotool` focuses the window and
   clicks (fallback).
3. **screenshot**: when not even an element can be found (grey area) → fall back to pixels, with the
   LLM making the visual decision.

The returned `ActionResult` states which tier actually took effect.

---

## 9. Project layout

```
cc-computer-use/
├── src/computer_use_mcp/
│   ├── _bootstrap.py        # runs first: sets GI_TYPELIB_PATH before importing gi
│   ├── server.py            # MCP entry + tool registration + --selftest
│   ├── backend/
│   │   ├── base.py          # Backend ABC + Rect/UINode/Element/TextBlock dataclasses
│   │   └── linux/
│   │       ├── atspi.py     # AT-SPI reads + do_action/set_value
│   │       ├── inject/      # xdotool injection + window geometry
│   │       │                #   (apps/base/keyboard/pointer/screens/windows)
│   │       ├── grab.py      # screen capture (shared by screenshot and OCR)
│   │       ├── ocr.py       # grey-area sensing: tesseract → text blocks with coordinates
│   │       ├── ring.py      # visual click ring (feedback for the human)
│   │       └── backend.py   # LinuxBackend composite implementation
│   ├── core/
│   │   ├── serializer.py    # tree serialization (denoise / save tokens / allocate refs)
│   │   ├── geometry.py      # coordinate calibration
│   │   ├── screen_lock.py   # screen-exclusive lock (mutual exclusion for injection)
│   │   ├── display/         # isolation sandbox (Xephyr + private AT-SPI bus +
│   │   │                    #   DISPLAY provisioning + yielding to the user)
│   │   └── coordinator/     # three-tier fallback orchestration
│   ├── tools/               # ui_tree/find/action/layout/screenshot/screen_text/windows/apps
│   └── utils/               # refs/errors/logging/blocks/temps
├── tests/                   # unit tests + zenity end-to-end + manual_* stories
├── demo/                    # Phase 0 feasibility verification
├── docs/                    # deployment guide
├── entry.py                 # PyInstaller entry point
├── build.sh                 # local packaging script (onedir + wrapper)
├── packaging/               # distribution: .deb / .mcpb
│   ├── build-in-container.sh  #   whole pipeline, runs inside ubuntu:22.04
│   ├── assemble.sh            #   lay out the distributable tree + manifest
│   ├── vendor_libs.py         #   dependency closure for the 9 bundled binaries, RPATH
│   ├── embed-atspi.sh         #   libatspi + Atspi typelib closure → _internal/
│   └── deb/                   #   control files, postinst, setup/doctor scripts, verification
└── pyproject.toml
```

The `core/display`, `core/coordinator` and `backend/linux/inject` packages were split out of single
modules by responsibility. Their public namespaces are unchanged (`display.MANAGER`, `inject.XdotoolInjector`,
`coordinator.Coordinator` are re-exported from `__init__.py`), so every existing import site kept working.

---

## 10. Known limitations

- **Scenes with no accessibility tree** (custom-drawn widgets, games, video, remote desktops, DRM):
  fall back to `get_screen_text`. Known limitation: a line of text immediately adjacent to a dark icon
  can fail to recognise — narrow the `region` and retry.
- **Coordinate drift**: some GTK dialogs return window-relative coordinates from `get_extents(SCREEN)`.
  The `geometry` layer compensates, and element-level `do_action` bypasses coordinates entirely.
- **Wayland**: the MVP targets X11; injection under Wayland is restricted and would need libei
  (under evaluation for Phase 2+).
- **uinput injection**: xdotool (XTest) is used today, which suffices for X11; uinput (needs a udev
  rule) is planned for Phase 2.3.
- **Non-ASCII input goes through the clipboard**: `xdotool type` rewrites the global keyboard mapping
  for CJK input, which makes the user's physical keyboard unresponsive for the duration and races with
  input-method XKB state. Only pure ASCII uses `xdotool type`.

---

## Roadmap

- ✅ **Phase 0** Feasibility verification (`demo/`)
- ✅ **Phase 1** Linux MVP (this project: 17 tools + backend + core + packaging)
- ✅ **Phase 1.5** Isolation sandbox (Xephyr virtual screen by default + private AT-SPI bus + `launch_app`)
- ⬜ **Phase 2** Precision hardening + token optimization (tree diffing, uinput, focus handling)
- ⬜ **Phase 3** Windows backend (UIA + SendInput)
- ⬜ **Phase 4** Visual parsing + further grey-area fallbacks

---

## License

[MIT](LICENSE) — Copyright (c) 2026 刘飞 (liufei)