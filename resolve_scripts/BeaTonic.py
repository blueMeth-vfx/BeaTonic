"""
BeaTonic - GUI per DaVinci Resolve (Workspace > Scripts > Utility > BeaTonic)

Montaggio ritmato a tempo di musica:
  1) Selezioni nel Media Pool le CLIP video (sorgenti) + la CANZONE (clip audio).
  2) "Analizza": ffmpeg rileva i cambi di scena nelle clip (le spezza in shot) e
     i BEAT della canzone.
  3) Scegli stile, durata e formato (orizzontale/verticale).
  4) "Monta": crea una timeline con la canzone e le shot tagliate SUI BEAT.

Richiede ffmpeg (rilevato in automatico). Self-contained, zero dipendenze Python.
"""

import math
import os
import re
import struct
import subprocess
import wave

_LOGO = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design",
                     "DaVinci Resolve", "Support", "Fusion", "Scripts", "Utility",
                     "plutonic_logo.png")

# --------------------------------------------------------------------------
# Connessione a Resolve / Fusion
# --------------------------------------------------------------------------
try:
    resolve
except NameError:
    import DaVinciResolveScript as _dvr
    resolve = _dvr.scriptapp("Resolve")

fusion = resolve.Fusion()
ui = fusion.UIManager
try:
    disp = bmd.UIDispatcher(ui)
except NameError:
    import fusionscript as bmd
    disp = bmd.UIDispatcher(ui)

_NOWIN = 0x08000000 if os.name == "nt" else 0   # niente console che lampeggia


def find_ffmpeg():
    cands = [r"D:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe", "ffmpeg"]
    env = os.environ.get("FFMPEG")
    if env:
        cands.insert(0, env)
    for c in cands:
        try:
            subprocess.run([c, "-version"], capture_output=True, creationflags=_NOWIN)
            return c
        except Exception:
            continue
    return None


FFMPEG = find_ffmpeg()
_TMP = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "BeatCut")
os.makedirs(_TMP, exist_ok=True)

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif", ".wma")


# --------------------------------------------------------------------------
# Analisi: scene cut (ffmpeg) + beat (ffmpeg decode + DSP)
# --------------------------------------------------------------------------
def scene_cuts(path, thresh=0.3):
    """Tempi (s) dei cambi di scena nel file."""
    if not FFMPEG:
        return []
    p = subprocess.run([FFMPEG, "-i", path, "-vf",
                        "select='gt(scene,%s)',metadata=print:file=-" % thresh,
                        "-an", "-f", "null", "-"],
                       capture_output=True, text=True, creationflags=_NOWIN)
    txt = (p.stdout or "") + (p.stderr or "")
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", txt)]


def _decode_mono(path, sr=11025):
    out = os.path.join(_TMP, "_song.wav")
    subprocess.run([FFMPEG, "-y", "-i", path, "-ac", "1", "-ar", str(sr), out],
                   capture_output=True, creationflags=_NOWIN)
    w = wave.open(out, "rb")
    raw = w.readframes(w.getnframes())
    w.close()
    data = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return [x / 32768.0 for x in data], sr


def _onset_rms(sig, sr, hop=128):
    E, RMS = [], []
    for i in range(0, len(sig) - hop, hop):
        e = 0.0
        for j in range(i, i + hop):
            e += sig[j] * sig[j]
        E.append(e)
        RMS.append((e / hop) ** 0.5)
    env = [max(0.0, E[k] - E[k - 1]) for k in range(1, len(E))]  # flusso d'energia
    return env, RMS[1:], sr / hop


def beat_grid(path):
    """Segue i TRANSIENTI della forma d'onda (non una griglia a tempo fisso): cosi'
    si adatta ai cambi di BPM e, grazie al GATE sull'energia, nei punti in cui la
    canzone sfuma/tace NON mette onset. Ritorna (bpm_medio, [tempi onset in s])."""
    sig, sr = _decode_mono(path)
    env, rms, fps = _onset_rms(sig, sr)
    if len(env) < 10:
        return 0.0, []
    envmax = max(env) or 1.0
    med = sorted(r for r in rms if r > 0)
    rms_med = med[len(med) // 2] if med else 0.0
    gate = 0.12 * rms_med            # sotto = silenzio/sfumato -> niente onset
    win = max(1, int(fps * 0.8)); w = 3
    onsets = []
    for i in range(w, len(env) - w):
        if rms[i] < gate:
            continue
        if env[i] < max(env[i - w:i + w + 1]):
            continue                 # non e' un massimo locale
        a = max(0, i - win); b = min(len(env), i + win)
        loc = env[a:b]; m = sum(loc) / len(loc)
        sd = (sum((x - m) ** 2 for x in loc) / len(loc)) ** 0.5
        if env[i] > m + 1.2 * sd and env[i] > 0.02 * envmax:
            onsets.append(i / fps)
    times = []
    for t in onsets:                 # togli doppioni troppo vicini
        if not times or t - times[-1] > 0.15:
            times.append(round(t, 3))
    if len(times) > 2:
        avg = sum(times[i + 1] - times[i] for i in range(len(times) - 1)) / (len(times) - 1)
        bpm = 60.0 / avg if avg else 0.0
    else:
        bpm = 0.0
    # INTENSITA' per onset: energia RMS locale (smussata ~1s) normalizzata 0..1.
    # Serve allo stile "dinamico" per seguire i sali/scendi del brano.
    cs = [0.0]
    for v in rms:
        cs.append(cs[-1] + v)
    wsm = max(1, int(fps * 0.5))

    def _loc(fr):
        a = max(0, fr - wsm); b = min(len(rms), fr + wsm + 1)
        return (cs[b] - cs[a]) / (b - a) if b > a else 0.0

    oe = [_loc(min(len(rms) - 1, int(t * fps))) for t in times]
    if oe:
        srt = sorted(oe)
        lo = srt[int(0.1 * len(srt))]
        hi = srt[min(len(srt) - 1, int(0.9 * len(srt)))]
        rng = (hi - lo) or 1.0
        intens = [max(0.0, min(1.0, (e - lo) / rng)) for e in oe]
    else:
        intens = []
    return bpm, times, intens


# --------------------------------------------------------------------------
# Media pool: selezione -> clip video + canzone
# --------------------------------------------------------------------------
def _is_audio(c):
    try:
        path = c.GetClipProperty("File Path") or ""
    except Exception:
        path = ""
    if os.path.splitext(path)[1].lower() in AUDIO_EXT:
        return True
    try:
        res = (c.GetClipProperty("Resolution") or "").strip()
        return res in ("", "0x0") and bool(c.GetClipProperty("Audio Ch"))
    except Exception:
        return False


def selection():
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool() if proj else None
    sel = []
    try:
        s = mp.GetSelectedClips()
        sel = list(s.values()) if isinstance(s, dict) else list(s or [])
    except Exception:
        sel = []
    videos, song = [], None
    for c in sel:
        if (c.GetClipProperty("Type") or "").lower() == "timeline":
            continue
        if _is_audio(c):
            if song is None:
                song = c
        else:
            videos.append(c)
    return videos, song


def _clip_fps(c):
    try:
        f = float(c.GetClipProperty("FPS"))
        return f if f > 0 else 25.0
    except Exception:
        return 25.0


def _clip_dur_sec(c):
    try:
        fr = int(c.GetClipProperty("Frames"))
        return fr / _clip_fps(c) if fr else 0.0
    except Exception:
        return 0.0


def build_shots(videos, thresh, set_log):
    """Spezza ogni clip ai cambi di scena -> lista di shot {clip,fps,in,out}."""
    shots = []
    for i, c in enumerate(videos, 1):
        path = c.GetClipProperty("File Path") or ""
        fps = _clip_fps(c)
        dur = _clip_dur_sec(c)
        set_log("Analisi scene %d/%d: %s…" % (i, len(videos), c.GetName()))
        cuts = scene_cuts(path, thresh) if path else []
        bounds = [0.0] + [t for t in cuts if 0.2 < t < dur - 0.1] + [dur]
        for a, b in zip(bounds, bounds[1:]):
            if b - a >= 0.4:   # scarta frammenti sotto 0.4s
                shots.append({"clip": c, "fps": fps, "in": a, "out": b})
    return shots


# --------------------------------------------------------------------------
# Montaggio (tape continuo di shot, tagliato sui beat)
# --------------------------------------------------------------------------
STYLES = {
    "Veloce (ogni beat)": [1],
    "Medio (ogni 2 beat)": [2],
    "Lento (ogni 4 beat)": [4],
    "Dinamico (segue l'energia)": None,   # densità dei tagli guidata dall'intensità
}
FORMATS = {
    "Orizzontale 16:9": (1920, 1080),
    "Verticale 9:16": (1080, 1920),
}


def _intervals(beats, style, dur, intens):
    pat = STYLES.get(style)
    out = []
    j, pi = 0, 0
    while j < len(beats) - 1 and beats[j] < dur:
        if pat:                       # stili fissi: veloce/medio/lento
            mult = pat[pi % len(pat)]; pi += 1
        else:                         # DINAMICO: densità dall'energia locale
            v = intens[j] if j < len(intens) else 0.5
            mult = 1 if v >= 0.6 else (2 if v >= 0.33 else 4)
        k = min(j + mult, len(beats) - 1)
        end = min(beats[k], dur)
        if end - beats[j] > 0.08:
            out.append((beats[j], end))
        j = k
    return out


def montage(shots, song, beats, style, dur, fmt, shuffle, reuse, intens, set_log):
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()
    if not shots:
        return "Nessuna shot: analizza prima le clip."
    if not beats or len(beats) < 2:
        return "Beat non rilevati: controlla la canzone."
    order = list(range(len(shots)))
    if shuffle:
        import random
        random.shuffle(order)
    tape = [shots[i] for i in order]

    new = mp.CreateEmptyTimeline("BeatCut_%s" % style.split()[0])
    n = 2
    while new is None and n < 100:
        new = mp.CreateEmptyTimeline("BeatCut_%s_%d" % (style.split()[0], n)); n += 1
    if new is None:
        return "Impossibile creare la timeline."
    # formato
    W, H = FORMATS.get(fmt, (1920, 1080))
    try:
        new.SetSetting("useCustomSettings", "1")
        new.SetSetting("timelineResolutionWidth", str(W))
        new.SetSetting("timelineResolutionHeight", str(H))
    except Exception:
        pass
    proj.SetCurrentTimeline(new)
    tl_fps = _tl_fps(new)
    ns = new.GetStartFrame()

    ivs = _intervals(beats, style, dur, intens)
    # "tape" continuo: posizione globale su tape[ti] a partire da shot['in']
    ti = [0]
    pos = [tape[0]["in"]] if tape else [0.0]
    exhausted = [False]

    def pull(length):
        """Lista di (clip, fps, src_in, src_out) lunga ~length s, scorrendo il tape.
        Se 'reuse' e' spento, si ferma quando il materiale finisce (niente ripetizioni)."""
        pieces = []
        need = length
        guard = 0
        while need > 0.02 and guard < 10000:
            guard += 1
            sh = tape[ti[0] % len(tape)]
            avail = sh["out"] - pos[0]
            if avail <= 0.02:
                ti[0] += 1
                if not reuse and ti[0] >= len(tape):
                    exhausted[0] = True
                    break
                pos[0] = tape[ti[0] % len(tape)]["in"]
                continue
            take = min(need, avail)
            pieces.append((sh["clip"], sh["fps"], pos[0], pos[0] + take))
            pos[0] += take
            need -= take
        return pieces

    ok = 0
    total_sec = 0.0
    for (bs, be) in ivs:
        pieces = pull(be - bs)
        if not pieces:
            break
        for clip, cfps, si, so in pieces:
            sf = int(round(si * cfps)); ef = int(round(so * cfps))
            if ef <= sf:
                ef = sf + 1
            info = {"mediaPoolItem": clip, "startFrame": sf, "endFrame": ef,
                    "trackIndex": 1, "mediaType": 1}
            try:
                if mp.AppendToTimeline([info]):   # SEQUENZIALE = gapless, sui beat
                    ok += 1
                    total_sec += (so - si)
            except Exception:
                pass
        if exhausted[0]:
            break
    # canzone su A1, dall'inizio, lunga quanto il montaggio
    try:
        send = int(round(total_sec * tl_fps))
        mp.AppendToTimeline([{"mediaPoolItem": song, "startFrame": 0, "endFrame": send,
                              "recordFrame": ns, "trackIndex": 1, "mediaType": 2}])
    except Exception:
        pass
    return ("Montaggio '%s' creato: %d stacchi su ~%.0fs, stile %s, %s."
            % (new.GetName(), ok, total_sec, style.split()[0], fmt))


def _tl_fps(tl):
    try:
        f = float(tl.GetSetting("timelineFrameRate"))
    except Exception:
        f = 25.0
    for a, e in ((23.976, 24000 / 1001), (29.97, 30000 / 1001), (59.94, 60000 / 1001)):
        if abs(f - a) < 0.01:
            return e
    return f


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
# Temi Plutonic: Scuro (Plutonic Studio) / Chiaro (Plutonic Labs).
# La scelta e' ricordata (condivisa con PlutoniCut) in %APPDATA%\Plutonic\theme.txt
_THEME_FILE = os.path.join(os.environ.get("APPDATA", ""), "Plutonic", "theme.txt")


def load_theme():
    try:
        with open(_THEME_FILE, "r") as f:
            t = f.read().strip()
        return t if t in ("studio", "labs") else "studio"
    except Exception:
        return "studio"


def save_theme(name):
    try:
        os.makedirs(os.path.dirname(_THEME_FILE), exist_ok=True)
        with open(_THEME_FILE, "w") as f:
            f.write(name)
    except Exception:
        pass


_PAL = {
    "studio": {
        "font": "'Segoe UI','Inter',sans-serif", "hfont": "'Segoe UI','Inter',sans-serif",
        "bg": "#0f1115", "panel": "#111827", "tree": "#0b0d11", "fg": "#e5e7eb",
        "logfg": "#cbd2da", "muted": "#9ca3af", "border": "#1f2937", "border2": "#374151",
        "bw": "1px", "radius": "8px", "accent": "#7c3aed", "focus": "#8b5cf6",
        "header": "#a78bfa", "selfg": "#ffffff",
        "primaryBg": "#7c3aed", "primaryHover": "#8b5cf6",
        "primaryFg": "#ffffff", "primaryBorder": "border:0;",
    },
    "labs": {
        "font": "'Public Sans','Segoe UI',sans-serif", "hfont": "'Rubik Mono One','Impact',sans-serif",
        "bg": "#F3F0F7", "panel": "#FBF4FF", "tree": "#FBF4FF", "fg": "#0E0418",
        "logfg": "#24103F", "muted": "#4B4458", "border": "#0E0418", "border2": "#0E0418",
        "bw": "2px", "radius": "6px", "accent": "#EE3A5B", "focus": "#EE3A5B",
        "header": "#B0126E", "selfg": "#ffffff",
        "primaryBg": "qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #F7A541, stop:1 #EE3A5B)",
        "primaryHover": "qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #F4F07C, stop:1 #F7A541)",
        "primaryFg": "#0E0418", "primaryBorder": "border:2px solid #0E0418;",
    },
}

_TMPL = """
* { font-family:%(font)s; font-size:13px; color:%(fg)s; outline:none; }
QWidget { background-color:%(bg)s; }
QLabel#Header { font-family:%(hfont)s; font-size:20px; font-weight:800; color:%(header)s; }
QPushButton#Logo { background:transparent; border:0; }
QPushButton#ThemeBtn { background:transparent; border:%(bw)s solid %(border2)s; border-radius:%(radius)s; padding:4px 9px; color:%(muted)s; font-weight:700; }
QPushButton#ThemeBtn:hover { border:%(bw)s solid %(accent)s; color:%(fg)s; }
QLabel#Src { color:%(muted)s; padding:4px 2px; }
QLineEdit, QComboBox { background:%(panel)s; border:%(bw)s solid %(border)s; border-radius:%(radius)s; padding:6px 9px; selection-background-color:%(accent)s; selection-color:%(selfg)s; }
QLineEdit:focus, QComboBox:focus { border:%(bw)s solid %(focus)s; }
QComboBox::drop-down { border:0; width:18px; }
QPushButton { background:%(panel)s; border:%(bw)s solid %(border2)s; border-radius:%(radius)s; padding:7px 13px; }
QPushButton:hover { background:%(border)s; }
QPushButton:pressed { background:%(tree)s; }
QPushButton#Analyze, QPushButton#Make { background:%(primaryBg)s; color:%(primaryFg)s; font-weight:700; %(primaryBorder)s padding:9px 13px; }
QPushButton#Analyze:hover, QPushButton#Make:hover { background:%(primaryHover)s; }
QPushButton#Close { color:%(muted)s; }
QCheckBox { spacing:6px; }
QCheckBox::indicator { width:16px; height:16px; border-radius:5px; border:%(bw)s solid %(border2)s; background:%(panel)s; }
QCheckBox::indicator:checked { background:%(accent)s; border:%(bw)s solid %(accent)s; }
QTextEdit { background:%(tree)s; border:%(bw)s solid %(border)s; border-radius:10px; color:%(logfg)s; padding:7px 9px; }
QLabel#Status { color:%(muted)s; background:%(tree)s; border:%(bw)s solid %(border)s; border-radius:%(radius)s; padding:7px 10px; }
"""

STYLE_FOR = {name: _TMPL % pal for name, pal in _PAL.items()}
THEME = load_theme()
STYLE = STYLE_FOR[THEME]

win = disp.AddWindow({
    "ID": "BeaTonicWin", "WindowTitle": "BeaTonic", "Geometry": [120, 90, 940, 560],
    "StyleSheet": STYLE,
}, [
    ui.VGroup({"Spacing": 7, "Margin": 14}, [
        ui.HGroup({"Weight": 0, "Spacing": 10}, [
            ui.Button({"ID": "Logo", "Flat": True, "Weight": 0,
                       "MinimumSize": [62, 48], "MaximumSize": [62, 48]}),
            ui.Label({"ID": "Header", "Text": "BeaTonic", "Weight": 0}),
            ui.Label({"Text": "montaggio a tempo di musica", "Weight": 1,
                      "Alignment": {"AlignBottom": True}}),
            ui.Button({"ID": "ThemeBtn", "Text": "☀  Tema Labs", "Weight": 0}),
        ]),
        ui.Label({"ID": "Src", "Text": "Seleziona clip + canzone nel Media Pool, poi Aggiorna.",
                  "Weight": 0}),

        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Button({"ID": "Refresh", "Text": "Aggiorna selezione", "Weight": 4}),
            ui.Label({"Text": "Sensib. scene", "Weight": 2}),
            ui.ComboBox({"ID": "Sens", "Weight": 3}),
            ui.Button({"ID": "Analyze", "Text": "▶  Analizza", "Weight": 4}),
        ]),

        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.ComboBox({"ID": "Style", "Weight": 5}),
            ui.ComboBox({"ID": "Fmt", "Weight": 4}),
            ui.Label({"Text": "Durata s", "Weight": 1}),
            ui.LineEdit({"ID": "Dur", "PlaceholderText": "30", "Weight": 1}),
            ui.CheckBox({"ID": "Shuffle", "Text": "casuale", "Weight": 2}),
            ui.CheckBox({"ID": "Reuse", "Text": "riusa clip", "Checked": True, "Weight": 2}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Button({"ID": "Make", "Text": "▶  Monta a tempo", "Weight": 4}),
            ui.Button({"ID": "Close", "Text": "Chiudi", "Weight": 1}),
        ]),

        ui.TextEdit({"ID": "Log", "Weight": 1, "ReadOnly": True,
                     "Text": "1) Seleziona clip + canzone  2) Analizza  3) Monta."}),
        ui.Label({"ID": "Status", "Text": "Pronto.", "Weight": 0, "WordWrap": True}),
    ]),
])

itm = win.GetItems()
try:
    if os.path.exists(_LOGO):
        itm["Logo"].Icon = ui.Icon({"File": _LOGO})
        itm["Logo"].IconSize = [58, 44]
except Exception:
    pass
for c in ["0.3 (media)", "0.2 (sensibile)", "0.4 (poco)", "0.5 (minima)"]:
    itm["Sens"].AddItem(c)
for s in STYLES:
    itm["Style"].AddItem(s)
for f in FORMATS:
    itm["Fmt"].AddItem(f)

VIDEOS, SONG, SHOTS, BEATS, BPM, INTENS = [], None, [], [], 0.0, []


def set_status(m):
    itm["Status"].Text = m


def set_log(m):
    try:
        itm["Log"].PlainText = m
    except Exception:
        try:
            itm["Log"].Text = m
        except Exception:
            pass


def on_refresh(ev=None):
    global VIDEOS, SONG
    VIDEOS, SONG = selection()
    itm["Src"].Text = "Selezione: %d clip video · canzone: %s" % (
        len(VIDEOS), SONG.GetName() if SONG else "— (nessuna)")
    if not FFMPEG:
        set_log("ffmpeg NON trovato. Installalo o imposta la variabile FFMPEG col percorso.")


def on_analyze(ev):
    global SHOTS, BEATS, BPM, INTENS
    on_refresh()
    if not FFMPEG:
        set_log("ffmpeg non trovato: impossibile analizzare."); return
    if not VIDEOS:
        set_log("Nessuna clip video selezionata nel Media Pool."); return
    if not SONG:
        set_log("Nessuna canzone (clip audio) selezionata nel Media Pool."); return
    sens = float((itm["Sens"].CurrentText or "0.3").split()[0])
    set_status("Analisi in corso (la finestra resta ferma)…")
    SHOTS = build_shots(VIDEOS, sens, set_log)
    set_log("Scene analizzate: %d shot totali.\nAnalizzo i beat della canzone…" % len(SHOTS))
    BPM, BEATS, INTENS = beat_grid(SONG.GetClipProperty("File Path") or "")
    set_log("ANALISI FATTA.\n• %d shot dalle clip (cambi scena).\n• Canzone: %.0f BPM, %d beat.\n\n"
            "Ora scegli stile/durata/formato e premi 'Monta a tempo'."
            % (len(SHOTS), BPM, len(BEATS)))
    set_status("Analisi ok: %d shot · %.0f BPM · %d beat." % (len(SHOTS), BPM, len(BEATS)))


def on_make(ev):
    if not SHOTS or not BEATS:
        set_log("Prima premi 'Analizza'."); return
    try:
        dur = float((itm["Dur"].Text or "30").replace(",", "."))
    except ValueError:
        dur = 30.0
    set_status("Montaggio in corso…")
    msg = montage(SHOTS, SONG, BEATS, itm["Style"].CurrentText, dur,
                  itm["Fmt"].CurrentText, bool(itm["Shuffle"].Checked),
                  bool(itm["Reuse"].Checked), INTENS, set_log)
    set_log(msg)
    set_status(msg[:80])


def on_close(ev):
    disp.ExitLoop()


win.On.Refresh.Clicked = on_refresh
win.On.Analyze.Clicked = on_analyze
win.On.Make.Clicked = on_make
win.On.Close.Clicked = on_close
win.On.BeaTonicWin.Close = on_close


def apply_theme(name):
    global THEME
    THEME = name
    done = False
    try:
        win.SetStyleSheet(STYLE_FOR[name]); done = True
    except Exception:
        pass
    if not done:
        try:
            itm["BeaTonicWin"].SetStyleSheet(STYLE_FOR[name])
        except Exception:
            pass
    try:
        itm["ThemeBtn"].Text = ("☀  Tema Labs" if name == "studio"
                                else "☾  Tema Studio")
    except Exception:
        pass
    save_theme(name)


def on_theme(ev=None):
    apply_theme("labs" if THEME == "studio" else "studio")


win.On.ThemeBtn.Clicked = on_theme
apply_theme(THEME)

on_refresh()
if not FFMPEG:
    set_status("ffmpeg non trovato.")
win.Show()
disp.RunLoop()
win.Hide()
