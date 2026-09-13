"""Mi-Ripple Studio — self-contained offline Gradio interface for Mi-Ripple.

Wraps the vendored ``mi_ripple`` diagnosis-guided restoration pipeline in a
local Gradio web app. Designed to be launched by the Electron shell on
127.0.0.1 with a caller-chosen port, but also works standalone:

    python app/gradio_app.py --port 7860

The app is fully offline: no telemetry, no external fetches, and the optional
paid regeneration route (which needs network + MIYANG API key) is disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# Ensure the vendored mi_ripple package (next to this file) is importable
# regardless of how the app is launched (venv, PyInstaller, electron).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from scipy import ndimage  # noqa: E402
from skimage import color as skcolor  # noqa: E402

import gradio as gr  # noqa: E402

from mi_ripple.pipeline import run as pipeline_run  # noqa: E402

APP_NAME = "Mi-Ripple Studio"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Synthetic example generation (deterministic, inlined from Mi-Ripple's test
# suite so the app stays self-contained).
# ---------------------------------------------------------------------------

_SYNT_H, _SYNT_W = 384, 512


def _lab_to_rgb8(lightness: np.ndarray, a: float = 12.0, b: float = 18.0):
    lab = np.stack(
        [np.clip(lightness, 0, 100), np.full_like(lightness, a), np.full_like(lightness, b)],
        axis=-1,
    )
    return np.clip(np.rint(skcolor.lab2rgb(lab) * 255), 0, 255).astype(np.uint8)


def _base_lightness(seed: int = 0):
    yy, xx = np.mgrid[0:_SYNT_H, 0:_SYNT_W].astype(np.float64)
    lightness = 62 + 12 * (xx / _SYNT_W) - 8 * (yy / _SYNT_H)
    stripes = 6 * np.sin((xx + 0.6 * yy) * 2 * np.pi / 14)
    mask = np.zeros_like(lightness)
    mask[_SYNT_H // 2 :, _SYNT_W // 2 :] = 1.0
    mask = ndimage.gaussian_filter(mask, 6)
    rng = np.random.default_rng(seed)
    return (
        lightness
        + stripes * mask
        + ndimage.gaussian_filter(rng.normal(0, 1.2, lightness.shape), 0.8)
    )


def synth_clean(seed: int = 0):
    return _lab_to_rgb8(_base_lightness(seed))


def synth_lattice(amplitude: float = 0.35, seed: int = 0):
    yy, xx = np.mgrid[0:_SYNT_H, 0:_SYNT_W]
    lattice = amplitude * (((xx + yy) % 2) * 2 - 1)
    lattice += 0.8 * amplitude * np.cos(2 * np.pi * xx / 4)
    return _lab_to_rgb8(_base_lightness(seed) + lattice)


def synth_granule(standard_deviation: float = 0.9, seed: int = 3):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:_SYNT_H, 0:_SYNT_W]
    granules = np.zeros((_SYNT_H, _SYNT_W))
    count = int(_SYNT_H * _SYNT_W / 40)
    cy = rng.integers(0, _SYNT_H, count)
    cx = rng.integers(0, _SYNT_W, count)
    radii = rng.uniform(2.0, 4.0, count)
    amps = rng.choice([-1.0, 1.0], count)
    for y0, x0, r, amp in zip(cy, cx, radii, amps, strict=True):
        y1, y2 = max(0, int(y0 - r - 1)), min(_SYNT_H, int(y0 + r + 2))
        x1, x2 = max(0, int(x0 - r - 1)), min(_SYNT_W, int(x0 + r + 2))
        granules[y1:y2, x1:x2] += amp * (
            np.hypot(yy[y1:y2, x1:x2] - y0, xx[y1:y2, x1:x2] - x0) <= r
        )
    granules = ndimage.gaussian_filter(granules, 0.7)
    granules = granules / granules.std() * standard_deviation
    return _lab_to_rgb8(_base_lightness(seed) + granules)


def synth_textured(seed: int = 5, amplitude: float = 2.5):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:_SYNT_H, 0:_SYNT_W]
    base = 55 + ndimage.gaussian_filter(rng.normal(0, 1, (_SYNT_H, _SYNT_W)), 5) * 60
    components = np.zeros((_SYNT_H, _SYNT_W))
    count = int(_SYNT_H * _SYNT_W / 45)
    cy = rng.integers(0, _SYNT_H, count)
    cx = rng.integers(0, _SYNT_W, count)
    radii = np.full(count, 3.0)
    aspects = rng.uniform(1.0, 3.5, count)
    angles = rng.uniform(0, np.pi, count)
    signs = rng.choice([-1.0, 1.0], count)
    for y0, x0, r, asp, ang, sgn in zip(cy, cx, radii, aspects, angles, signs, strict=True):
        extent = r * asp
        y1, y2 = max(0, int(y0 - extent - 1)), min(_SYNT_H, int(y0 + extent + 2))
        x1, x2 = max(0, int(x0 - extent - 1)), min(_SYNT_W, int(x0 + r + 2))
        dy = yy[y1:y2, x1:x2] - y0
        dx = xx[y1:y2, x1:x2] - x0
        u = dx * np.cos(ang) + dy * np.sin(ang)
        v = -dx * np.sin(ang) + dy * np.cos(ang)
        components[y1:y2, x1:x2] += sgn * ((u / (r * asp)) ** 2 + (v / r) ** 2 <= 1)
    components = ndimage.gaussian_filter(components, 0.7)
    components = components / components.std() * amplitude
    return _lab_to_rgb8(base + components)


SYNTH_FACTORY = {
    "Clean (control)": lambda seed, amp: synth_clean(seed),
    "Lattice (periodic grid)": lambda seed, amp: synth_lattice(amp, seed),
    "Granules (3-8 px texture)": lambda seed, amp: synth_granule(amp, seed),
    "Texture (content-entangled)": lambda seed, amp: synth_textured(seed, amp),
}

# ---------------------------------------------------------------------------
# Paths & context
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, data_dir: Path, examples_dir: Path) -> None:
        self.data_dir = data_dir
        self.examples_dir = examples_dir
        self.work_dir = data_dir / "work"
        self.runs_dir = data_dir / "runs"
        for d in (self.data_dir, self.examples_dir, self.work_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)


CTX: Ctx | None = None
# Component id of the "Restore" tab (set in build_ui). Gradio 5 selects tabs
# by tab-item id, so programmatic tab switching must return this id.
RESTORE_TAB_ID: int | None = None

FRIENDLY_STEP = {
    "diagnose": "Diagnosing — measuring lattice, granule and scale signatures",
    "iso_clean": "Treating — reducing flat-region granules (strict mask)",
    "notch": "Treating — notching isolated periodic spectral peaks",
    "verify": "Verifying — comparing aligned distortion vs. the original",
    "reference_clean": "Cleaning reference (regeneration route — offline: skipped)",
    "regenerate": "Regenerating (offline build: this step is unavailable)",
    "request_human": "Routing to human review",
    "finish": "Finalizing",
}

OUTCOME_RENDER = {
    "delivered": (
        "✅ **Delivered** — a restored candidate was produced by the deterministic "
        "route. This is a *candidate*: review the boards below and accept it yourself."
    ),
    "delivered_with_verify_failure": (
        "⚠️ **Delivered with verification failure** — the masked granule-reduction "
        "candidate did not pass the verification gate, so the pipeline rolled back to "
        "the low-distortion notch-only candidate. Inspect the verification board."
    ),
    "needs_human_decision": (
        "🔶 **Human decision required** — artifacts are entangled with legitimate image "
        "content (hair, foliage, fabric…). The offline deterministic route stops here by "
        "design. In the full Mi-Ripple tool this is where *optional, paid, online* "
        "regeneration from a cleaned reference can be requested — that network route is "
        "**intentionally disabled in this offline build**. Review the evidence below and "
        "decide whether the original or a manual edit is preferable."
    ),
    "failed": "❌ **Failed** — an error occurred inside the pipeline.",
    "step_limit": "⚠️ **Step limit reached** — the pipeline did not finish in time.",
    "cancelled": "⚪ **Cancelled**.",
}


def _ensure_examples() -> None:
    """Generate bundled synthetic examples (once) and rebuild the index.

    The index is rebuilt on every startup so that example assets added to the
    bundled directory (or generated by the in-app generator) always show up.
    """
    assert CTX is not None
    entries = []
    specs = [
        ("synthetic_lattice", "Lattice — periodic grid artifact", lambda: synth_lattice(0.35, 0)),
        ("synthetic_granules", "Granules — 3–8 px texture", lambda: synth_granule(0.9, 3)),
        ("synthetic_texture", "Texture — content-entangled scales", lambda: synth_textured(5, 2.5)),
        ("synthetic_clean", "Clean — control (no artifact)", lambda: synth_clean(0)),
    ]
    for name, label, make in specs:
        path = CTX.examples_dir / f"{name}.png"
        if not path.exists():
            Image.fromarray(make()).resize((1024, 768), Image.LANCZOS).save(path)
        entries.append({"name": name, "label": label, "rel": path.name, "kind": "synthetic"})
    real_dir = CTX.examples_dir / "real"
    if real_dir.is_dir():
        for path in sorted(real_dir.iterdir()):
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                entries.append(
                    {
                        "name": path.stem,
                        "label": path.stem.replace("-", " ").title(),
                        "rel": f"real/{path.name}",
                        "kind": "reference",
                    }
                )
    for path in sorted(CTX.examples_dir.glob("generated_*.png")):
        entries.append(
            {"name": path.stem, "label": path.stem.replace("generated_", "").title(),
             "rel": path.name, "kind": "generated"}
        )
    (CTX.examples_dir / "index.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


def _examples() -> list[dict]:
    """Return the example list with absolute, existing paths."""
    _ensure_examples()
    index = CTX.examples_dir / "index.json"
    if not index.exists():
        return []
    entries = json.loads(index.read_text(encoding="utf-8"))
    out = []
    for e in entries:
        rel = e.get("rel") or e.get("path") or ""
        # Accept both the new relative form and any absolute legacy entry.
        abs_path = Path(rel)
        if not abs_path.is_absolute():
            abs_path = CTX.examples_dir / rel
        if abs_path.is_file():
            e["path"] = str(abs_path)
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _pick(run_dir: Path, stem: str, *suffixes) -> str | None:
    for suffix in suffixes:
        p = run_dir / f"{stem}_{suffix}.png"
        if p.exists():
            return str(p)
    return None


def process(
    image_path: str,
    max_side: float,
    progress=gr.Progress(),
) -> tuple:
    """Run the deterministic Mi-Ripple pipeline on one uploaded image."""
    t_start = time.time()
    source = Path(image_path)
    if not source.is_file():
        raise gr.Error("No image uploaded — pick a file first (PNG/JPEG/WebP).")

    stamp = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    stem = f"{stamp}_{source.stem}"
    run_dir = CTX.runs_dir / f"{stamp}_{source.stem}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Optional downscale of very large inputs (the pipeline is O(N) heavy).
    with Image.open(source) as img:
        img.load()
        w, h = img.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
        with Image.open(source) as img:
            work = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    else:
        with Image.open(source) as img:
            work = img.copy()
    work.convert("RGB").save(CTX.work_dir / f"{stem}.png")
    work_input = CTX.work_dir / f"{stem}.png"
    work_w, work_h = work.size

    state = {"n": 0}

    def on_event(event: dict) -> None:
        if event.get("type") == "step_start":
            state["n"] += 1
            action = event["step"].get("action", "")
            label = FRIENDLY_STEP.get(action, action)
            progress(min(0.05 + 0.9 * state["n"] / 8.0, 0.98), desc=label)

    summary = pipeline_run(
        work_input,
        run_dir,
        allow_regen=False,  # offline build: the paid regeneration route is disabled
        on_event=on_event,
    )
    elapsed = time.time() - t_start
    progress(1.0, desc="Done")

    outcome = summary["outcome"]
    banner = OUTCOME_RENDER.get(outcome, f"**{outcome}**")
    note = (summary.get("human_note") or "").strip()
    if note:
        banner += f"\n\n> {note}"
    if summary.get("final_is_source_copy"):
        banner += (
            "\n\n> No supported artifact class was detected, so the output is a copy "
            "of the input — the image already looks clean to the deterministic triage."
        )
    banner += (
        f"\n\n_⏱ {elapsed:.1f}s · {work_w}×{work_h} px · offline deterministic route_"
    )

    after = summary.get("final")
    if not (after and Path(after).exists()):
        after = None

    def text_of(name: str) -> str:
        p = run_dir / name
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")[:20000]
            except Exception as exc:  # pragma: no cover
                return f"(could not read {name}: {exc!r})"
        return ""

    diag_json = text_of(f"{stem}_input_diag.json") or text_of(f"{stem}_regen1_diag.json")
    summary_json = text_of(f"{stem}_restored.json")
    trace_xml = text_of(f"{stem}_trace.xml")

    verify_board = None
    if summary.get("verify") and summary["verify"].get("board"):
        verify_board = summary["verify"]["board"]
    verify_board = verify_board or _pick(run_dir, stem, "verify_board", "verify_rollback_board")

    zip_out = CTX.runs_dir / f"{run_dir.name}.zip"
    if not zip_out.exists():
        # make_archive appends ".zip" to base_name, so strip it first.
        shutil.make_archive(str(zip_out.with_suffix("")), "zip", root_dir=run_dir)

    return (
        banner,
        str(work_input),
        after,
        _pick(run_dir, stem, "input_diag_board", "regen1_diag_board"),
        _pick(run_dir, stem, "input_scaleheat", "regen1_scaleheat"),
        verify_board,
        trace_xml,
        diag_json,
        summary_json,
        str(zip_out),
    )


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


def load_example_by_label(label: str) -> tuple[str, str]:
    """Return (absolute example path, tab-switch trigger) for the label."""
    for e in _examples():
        if e["label"] == label:
            return (e["path"], _tab_trigger_html())
    if label:
        raise gr.Error(f"Example not found: {label}")
    return (None, _tab_trigger_html())


def generate_example(kind: str, amplitude: float, seed: int) -> tuple[str, str, str]:
    assert CTX is not None
    stamp = datetime.now().strftime("%H%M%S")
    out = CTX.examples_dir / f"generated_{kind[:20].replace(' ', '_')}_{stamp}.png"
    arr = SYNTH_FACTORY[kind](int(seed), float(amplitude))
    Image.fromarray(arr).resize((1024, 768), Image.LANCZOS).save(out)
    return (str(out), str(out), _tab_trigger_html())


# ---------------------------------------------------------------------------
# Offline hardening: strip Gradio's index.html of the two Google Fonts
# preconnect hints and the CDN-hosted iframe-resizer script. The app is meant
# to run with no network at all; these tags are the only external references.
# ---------------------------------------------------------------------------

_PRECONNECT_RE = re.compile(r'\s*<link\s+rel="preconnect"[^>]*/?>', re.S)
_IFRAME_RESIZER_RE = re.compile(
    r'\s*<script[^>]*cdnjs\.cloudflare\.com[^>]*>\s*</script>', re.S
)

# Tiny helper injected into the served page: switches tabs by clicking the
# tab button with the given data-tab-id. Needed because Gradio 5 layouts
# (Tabs) cannot be function outputs, so programmatic tab switching must
# happen client-side.
_TAB_HELPER_SCRIPT = (
    "<script>window.__mrs_openTab=function(id){var b=document.querySelector("
    "'[data-tab-id=\"'+id+'\"]');if(b){b.click();}};</script>"
)


def _rewrite_html(text: str) -> str:
    """Make the page fully offline + add the tab-switch helper (idempotent)."""
    text = _PRECONNECT_RE.sub("", text)
    text = _IFRAME_RESIZER_RE.sub("", text)
    if "window.__mrs_openTab" not in text:
        text = text.replace("</body>", _TAB_HELPER_SCRIPT + "</body>")
    return text


def _tab_trigger_html() -> str:
    """HTML fragment that switches to the Restore tab when injected.

    An img with an invalid src reliably fires onerror even inside a
    display:none container; the handler then clicks the Restore tab button.
    """
    tab_id = RESTORE_TAB_ID if RESTORE_TAB_ID is not None else "0"
    return (
        '<img src="data:," width="0" height="0" alt="" '
        f'onerror="window.__mrs_openTab&&window.__mrs_openTab({tab_id});this.remove()">'
    )


class OfflineHTMLRewriter:
    """ASGI middleware that rewrites the root HTML page to reference no external hosts."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in ("/", ""):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        captured: dict = {"rewrite": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message["headers"])
                ctype = dict(
                    (k.decode("latin1"), v.decode("latin1")) for k, v in headers
                ).get("content-type", "")
                if "text/html" in ctype:
                    captured["rewrite"] = True
                    captured["status"] = message["status"]
                    captured["headers"] = headers
                    return  # buffer the body, send it after rewriting
                await send(message)
            elif message["type"] == "http.response.body" and captured.get("rewrite"):
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    text = b"".join(chunks).decode("utf-8", errors="replace")
                    text = _rewrite_html(text)
                    new_body = text.encode("utf-8")
                    headers = [
                        (k, v)
                        for k, v in captured["headers"]
                        if k.lower() != b"content-length"
                    ]
                    headers.append((b"content-length", str(len(new_body)).encode()))
                    await send(
                        {
                            "type": "http.response.start",
                            "status": captured["status"],
                            "headers": headers,
                        }
                    )
                    await send({"type": "http.response.body", "body": new_body})
            else:
                await send(message)

        await self.app(scope, receive, send_wrapper)


def _install_offline_middleware(app) -> None:
    from starlette.middleware import Middleware

    # Starlette builds its middleware stack lazily on the first request, so
    # installing this right after launch() (before any client connects) works.
    app.user_middleware.insert(0, Middleware(OfflineHTMLRewriter))


def _patch_template_file() -> bool:
    """Patch gradio's index.html on disk (idempotent): offline-clean + helper.

    Returns True when the served template is clean (patched now or before).
    The Jinja FileSystemLoader re-reads on change, so this takes effect for
    the very first page load.
    """
    import gradio as gr_mod

    index = Path(gr_mod.__file__).parent / "templates" / "frontend" / "index.html"
    if not index.is_file():
        return False
    text = index.read_text(encoding="utf-8")
    patched = _rewrite_html(text)
    if patched == text:
        return True  # already fully patched
    try:
        index.write_text(patched, encoding="utf-8")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

ABOUT_MD = f"""
## What this is

**{APP_NAME}** is a fully self-contained, **offline-only** desktop shell around
[Mi-Ripple](https://github.com/miyang-ai/Mi-Ripple) — MIYANG's diagnosis-guided
workflow for restoring *digital ripple artifacts* (grid-like, scale-like, granular
patterns) introduced by iterative, reference-conditioned AI image editing.

It does **not** apply one aggressive filter to every image. It first separates:

1. **Periodic lattice artifacts** — isolated spectral peaks, selectively notched
   with low measured distortion.
2. **Granular artifacts in unstructured regions** — 3–8 px texture reduced behind a
   structure-protection mask.
3. **Content-entangled artifacts** — repeated texture overlapping hair, foliage,
   fabric, or other legitimate detail. These require human review (or optional
   *online* regeneration in the full tool — disabled here).

## How a run works

`diagnose → treat (masked spatial reduction / spectral notch) → verify → deliver`.
Every run writes a full evidence pack (diagnosis JSON, heat maps, masks,
verification boards, an XML decision trace, hashes) which you can download as a
zip from the Restore tab.

## Offline notice

- No telemetry, no external requests, no auto-updates. Everything ships with the app.
- The optional regeneration route requires a paid MIYANG API key **and** a network
  connection; it is **disabled** in this build. When the deterministic route stops
  with `needs_human_decision`, that is the expected offline behaviour — the
  generated evidence is yours to review.

## Read this before trusting scores

> This is a research implementation, not a universal artifact detector.
> Automatic scores are **triage signals**. Review the generated heat maps and
> comparison boards before accepting any output. Verification gates exist to
> bound distortion, not to guarantee semantic fidelity.

## Credits & license

- Mi-Ripple source: © 2026 MIYANG Technology (Shanghai) Co., Ltd., MIT License.
- MIYANG, its logo, and associated product branding are trademarks of MIYANG
  Technology (Shanghai) Co., Ltd.; this unofficial community shell does not grant
  rights to those marks.
- Gradio UI: © Hugging Face, Apache-2.0.

**Version {VERSION}**
"""


# ---------------------------------------------------------------------------
# Theme — "midnight precision instrument" (Linear-inspired).
# Near-black surfaces separated by hairline borders (no shadows) and one
# acid-lime accent used sparingly for the single primary action. Dark in both
# color schemes so the app looks identical regardless of OS theme.
# ---------------------------------------------------------------------------

VOID = "#08090a"        # page canvas
CARBON = "#0f1011"      # card surfaces
OBSIDIAN = "#161718"    # elevated surfaces (inputs, code, badges)
GRAPHITE = "#23252a"    # hairline borders
SMOKE = "#383b3f"       # hover borders
ASH = "#62666d"         # muted text / placeholders
FOG = "#8a8f98"         # tertiary text
MIST = "#d0d6e0"        # primary body text
BONE = "#e5e5e6"        # headings
ACID = "#e4f222"        # the single electric accent
ACID_HOVER = "#f0ff56"
ACID_TEXT_ON = "#0c0d03"
CORAL = "#eb5757"


def _color_scale(name: str, c500: str, c400: str, c600: str,
                 c300: str, c700: str, c50: str, c100: str,
                 c200: str, c800: str, c900: str, c950: str) -> "gr.themes.utils.colors.Color":
    from gradio.themes.utils import colors

    return colors.Color(
        name=name, c50=c50, c100=c100, c200=c200, c300=c300, c400=c400,
        c500=c500, c600=c600, c700=c700, c800=c800, c900=c900, c950=c950,
    )


# Gradio hues are full 50–950 scales; build them around the Linear palette.
ACID_HUE = _color_scale(
    "acid", c500=ACID, c400="#f0ff56", c600="#c2ce1d",
    c300="#edf76e", c700="#a0a918", c50="#fcfee3", c100="#f9fdc4",
    c200="#f6fa9a", c800="#757d11", c900="#4c520b", c950="#333707",
)
FOG_HUE = _color_scale(
    "fog", c500=FOG, c400="#a1a6ac", c600="#686c73",
    c300="#b8bcc0", c700="#4e5258", c50="#f6f7f8", c100="#e7e9eb",
    c200="#d0d3d6", c800="#35383c", c900="#232528", c950="#18191c",
)


_FONT_STACK = (
    # Linear's face is Inter; system-ui is its sanctioned offline fallback
    # (plain strings — no GoogleFont objects, so zero external font requests).
    "Inter",
    "system-ui",
    "-apple-system",
    "Segoe UI",
    "Roboto",
    "Helvetica Neue",
    "Arial",
    "sans-serif",
)
_FONT_MONO_STACK = (
    "ui-monospace",
    "SFMono-Regular",
    "Menlo",
    "Consolas",
    "monospace",
)


class MidnightTheme(gr.themes.Default):
    """Dark theme modelled on Linear's design system."""

    def __init__(self) -> None:
        super().__init__(
            primary_hue=ACID_HUE,
            secondary_hue=FOG_HUE,
            neutral_hue=FOG_HUE,
            font=_FONT_STACK,
            font_mono=_FONT_MONO_STACK,
        )

        def setd(var: str, val: str) -> None:
            setattr(self, var, val)
            setattr(self, var + "_dark", val)

        # --- surfaces -------------------------------------------------------
        for var, val in (
            ("background_fill_primary", VOID),
            ("background_fill_secondary", CARBON),
            ("body_background_fill", VOID),
            ("block_background_fill", CARBON),
            ("panel_background_fill", CARBON),
            ("stat_background_fill", OBSIDIAN),
            ("input_background_fill", OBSIDIAN),
            ("input_background_fill_hover", OBSIDIAN),
            ("input_background_fill_focus", OBSIDIAN),
            ("checkbox_background_color", OBSIDIAN),
            ("code_background_fill", OBSIDIAN),
            ("table_odd_background_fill", CARBON),
            ("table_even_background_fill", OBSIDIAN),
        ):
            setd(var, val)

        # --- text ------------------------------------------------------------
        for var, val in (
            ("body_text_color", MIST),
            ("body_text_color_subdued", FOG),
            ("block_info_text_color", FOG),
            ("input_placeholder_color", ASH),
            ("accordion_text_color", MIST),
            ("table_text_color", MIST),
            ("block_label_text_color", MIST),
            ("block_title_text_color", MIST),
        ):
            setd(var, val)
        self.body_text_weight = "400"
        self.block_label_text_weight = "500"
        self.block_title_text_weight = "500"

        # --- block label badges (the small caption pills) ----------------------
        for var, val in (
            ("block_label_background_fill", OBSIDIAN),
            ("block_title_background_fill", OBSIDIAN),
            ("block_label_border_color", GRAPHITE),
        ):
            setd(var, val)

        # --- hairline borders ---------------------------------------------------
        for var, val in (
            ("border_color_primary", GRAPHITE),
            ("border_color_accent", ACID),
            ("block_border_color", GRAPHITE),
            ("panel_border_color", GRAPHITE),
            ("table_border_color", GRAPHITE),
            ("input_border_color", GRAPHITE),
            ("input_border_color_hover", SMOKE),
            ("input_border_color_focus", ACID),
            ("button_secondary_border_color", GRAPHITE),
            ("button_secondary_border_color_hover", SMOKE),
            ("checkbox_border_color", GRAPHITE),
            ("checkbox_border_color_focus", ACID),
            ("error_border_color", CORAL),
        ):
            setd(var, val)
        setd("block_border_width", "1px")
        setd("input_border_width", "1px")

        # --- primary action: the single acid-lime "flashlight" ------------------
        for var, val in (
            ("button_primary_background_fill", ACID),
            ("button_primary_background_fill_hover", ACID_HOVER),
            ("button_primary_border_color", ACID),
            ("button_primary_border_color_hover", ACID_HOVER),
            ("button_primary_text_color", ACID_TEXT_ON),
            ("button_primary_text_color_hover", ACID_TEXT_ON),
        ):
            setd(var, val)

        # --- secondary / ghost buttons ------------------------------------------
        for var, val in (
            ("button_secondary_background_fill", OBSIDIAN),
            ("button_secondary_background_fill_hover", "#1e2023"),
            ("button_secondary_text_color", MIST),
            ("button_secondary_text_color_hover", BONE),
        ):
            setd(var, val)

        # --- accent: active tab, focus rings, progress bar ------------------------
        setd("color_accent", ACID)
        setd("color_accent_soft", "#20240b")
        setd("slider_color", ACID)

        # --- links: quiet until hover, then lime ----------------------------------
        for var, val in (
            ("link_text_color", MIST),
            ("link_text_color_hover", ACID),
            ("link_text_color_active", ACID),
            ("link_text_color_visited", MIST),
        ):
            setd(var, val)

        # --- errors: coral wash -----------------------------------------------------
        setd("error_background_fill", "rgba(235, 87, 87, 0.14)")
        setd("error_text_color", "#f2a1a1")

        # --- radius vocabulary: 4 / 6 / 12 -------------------------------------------
        self.radius_xs = "2px"
        self.radius_sm = "4px"
        self.radius_md = "6px"
        self.radius_lg = "12px"
        self.radius_xl = "12px"
        self.radius_xxl = "16px"
        self.block_radius = "*radius_lg"
        self.button_small_radius = "*radius_md"
        self.button_medium_radius = "*radius_md"
        self.button_large_radius = "*radius_md"
        self.input_radius = "*radius_md"
        self.checkbox_border_radius = "*radius_md"
        self.block_label_radius = "*radius_md"
        self.block_title_radius = "*radius_md"

        # --- no shadows: borders do the separation ------------------------------------
        for var in (
            "block_shadow",
            "block_label_shadow",
            "input_shadow",
            "input_shadow_focus",
            "button_primary_shadow",
            "button_secondary_shadow",
            "button_cancel_shadow",
            "checkbox_label_shadow",
            "checkbox_shadow",
            "shadow_drop",
            "shadow_drop_lg",
        ):
            setattr(self, var, "none")
            setattr(self, var + "_dark", "none")

        # Typography is set via the font=/font_mono= constructor args above
        # (that is what builds the CSS font stacks; plain strings keep the
        # served page free of any Google Fonts <link> — offline guarantee).


LINEAR_CSS = """
footer {visibility: hidden !important;}
.gradio-container {max-width: 1400px !important;}
.mrs-tab-trigger {display: none !important;}

/* --- Linear-flavoured refinements ----------------------------------------- */
html, body {background: #08090a;}
h1, h2, h3, h4 {letter-spacing: -0.012em; font-weight: 510;}
::selection {background: rgba(228, 242, 34, 0.28);}
::-webkit-scrollbar {width: 10px; height: 10px;}
::-webkit-scrollbar-track {background: transparent;}
::-webkit-scrollbar-thumb {
    background: #23252a; border-radius: 8px; border: 2px solid #08090a;
}
::-webkit-scrollbar-thumb:hover {background: #383b3f;}

/* image blocks sit on the card surface with a hairline edge */
.image-container {
    background: #0f1011 !important;
    border-color: #23252a !important;
    border-width: 1px !important;
}

/* gallery: machined thumbnails */
button.thumbnail-item {
    background: #161718;
    border: 1px solid #23252a !important;
    border-radius: 10px;
    overflow: hidden;
}
button.thumbnail-item:hover {border-color: #383b3f !important;}

/* quiet tab row; the acid underline marks the active one */
.tab {font-weight: 500;}
"""


def build_ui() -> gr.Blocks:
    examples = _examples()

    with gr.Blocks(
        title=APP_NAME,
        theme=MidnightTheme(),
        css=LINEAR_CSS,
        analytics_enabled=False,
    ) as demo:
        gr.Markdown(
            f"## 🌊 {APP_NAME} <span style='font-size:0.5em; color:#8a8f98;'>"
            "offline · self-contained · diagnosis-guided ripple restoration</span>"
        )

        # NOTE: do NOT pass selected= to gr.Tabs(). Gradio 5 selects tabs by
        # tab-item id; an unmatched int (e.g. 0) silently renders no tab body.
        with gr.Tabs() as tabs:
            # ---------------- Restore tab ----------------
            with gr.Tab("🛠 Restore"):
                with gr.Row():
                    with gr.Column(scale=3):
                        img_in = gr.Image(
                            label="Input image (PNG / JPEG / WebP)",
                            type="filepath",
                            sources=["upload", "clipboard"],
                        )
                        max_side = gr.Slider(
                            512, 4096, value=2048, step=128,
                            label="Downscale cap (longest side, px) — 4096 = native",
                        )
                        run_btn = gr.Button("▶ Run Mi-Ripple (offline)", variant="primary")
                    with gr.Column(scale=4):
                        outcome_md = gr.Markdown(
                            "_Upload an image (or open one from **Examples**) and press "
                            "**Run**. Typical runtime: ~5 s at 1024 px, ~20–35 s at 2048 px._"
                        )
                with gr.Row():
                    before_out = gr.Image(
                        label="Before (processed input)", interactive=False
                    )
                    after_out = gr.Image(
                        label="After (restored candidate)", interactive=False
                    )
                with gr.Row():
                    diag_board = gr.Image(label="Diagnosis board", interactive=False)
                    scale_heat = gr.Image(label="Scale-index heat map", interactive=False)
                verify_board_out = gr.Image(label="Verification board", interactive=False)
                with gr.Accordion("Evidence pack", open=False):
                    with gr.Row():
                        trace_code = gr.Code(label="Decision trace (XML)", language="html")
                        diag_code = gr.Code(label="Diagnosis JSON", language="json")
                    summary_code = gr.Code(label="Run summary JSON", language="json")
                zip_out = gr.File(label="Download full evidence pack (zip)")

            # ---------------- Examples tab ----------------
            with gr.Tab("🖼 Examples"):
                gr.Markdown(
                    "Bundled, offline demo images — **synthetic** ripples generated with "
                    "Mi-Ripple's own deterministic test generators, plus curated "
                    "reference assets. **Click a thumbnail** to load it into the "
                    "Restore tab."
                )
                ex_grid = gr.Gallery(
                    columns=3,
                    object_fit="cover",
                    height=240,
                    label="",
                    value=[(e["path"], e["label"]) for e in examples],
                    interactive=True,  # clicking a thumbnail also loads it
                )
                with gr.Row():
                    ex_select = gr.Dropdown(
                        choices=[e["label"] for e in examples],
                        value=examples[0]["label"] if examples else None,
                        label="Example",
                        interactive=True,
                    )
                    ex_load_btn = gr.Button("Open in Restore →", variant="secondary")
                # Hidden element updated after a load: its onerror fires and
                # clicks the Restore tab (Tabs can't be a function output).
                tab_trigger = gr.HTML("", elem_classes=["mrs-tab-trigger"])
                with gr.Accordion("Generate a synthetic ripple image", open=False):
                    with gr.Row():
                        gen_kind = gr.Dropdown(
                            list(SYNTH_FACTORY.keys()),
                            value="Lattice (periodic grid)",
                            label="Artifact type",
                        )
                        gen_amp = gr.Slider(
                            0.05, 2.0, value=0.9, step=0.05,
                            label="Amplitude / strength",
                        )
                        gen_seed = gr.Number(value=42, precision=0, label="Seed")
                        gen_btn = gr.Button("Generate", variant="secondary")
                    gen_preview = gr.Image(label="Generated (preview)", interactive=False)

            # ---------------- About tab ----------------
            with gr.Tab("ℹ About"):
                gr.Markdown(ABOUT_MD)

        # Wire up restore
        outputs = [
            outcome_md, before_out, after_out, diag_board, scale_heat,
            verify_board_out, trace_code, diag_code, summary_code, zip_out,
        ]
        run_btn.click(process, inputs=[img_in, max_side], outputs=outputs)

        # Wire up examples: loading an example sets the input image and
        # switches to the Restore tab via the hidden trigger element.
        def _gallery_pick(evt: gr.SelectData):
            entry = examples[evt.index]
            return (entry["path"], _tab_trigger_html())

        ex_grid.select(_gallery_pick, outputs=[img_in, tab_trigger])

        ex_load_btn.click(
            load_example_by_label,
            inputs=[ex_select],
            outputs=[img_in, tab_trigger],
        )

        gen_btn.click(
            generate_example,
            inputs=[gen_kind, gen_amp, gen_seed],
            outputs=[gen_preview, img_in, tab_trigger],
        )

    # Resolve the Restore tab's config id (Gradio 5 selects tabs by id).
    # The config is final once the Blocks context is closed.
    global RESTORE_TAB_ID
    for comp in demo.config["components"]:
        if (
            comp.get("type") == "tabitem"
            and "Restore" in str(comp.get("props", {}).get("label", ""))
        ):
            RESTORE_TAB_ID = comp["id"]
            break

    return demo


def main() -> None:
    parser = argparse.ArgumentParser(description="Mi-Ripple Studio (offline Gradio app)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--data-dir",
        default=str(Path.home() / ".mi-ripple-studio"),
        help="Where runs, work files and generated examples are stored",
    )
    parser.add_argument(
        "--examples-dir",
        default=str(_APP_DIR.parent / "examples"),
        help="Bundled example images (generated into it if empty)",
    )
    args = parser.parse_args()

    global CTX
    CTX = Ctx(Path(args.data_dir), Path(args.examples_dir))
    _ensure_examples()

    # Make the served page reference no external hosts (offline-only).
    template_clean = _patch_template_file()

    demo = build_ui()
    demo.queue(default_concurrency_limit=1)

    print(f"{APP_NAME} v{VERSION} starting on http://{args.host}:{args.port}", flush=True)
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        inbrowser=False,
        show_api=False,
        quiet=True,
        max_file_size="500mb",
        prevent_thread_lock=True,
    )
    if not template_clean:
        # Fallback: template file not writable — rewrite responses in flight.
        _install_offline_middleware(demo.app)
    # Signal the Electron shell (or any wrapper) that the server is up.
    print(f"MI_RIPPLE_READY {args.port}", flush=True)
    # Block forever; the parent process (Electron) owns our lifecycle.
    stop = threading.Event()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
