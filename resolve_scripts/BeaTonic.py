"""
BeaTonic - GUI per DaVinci Resolve (Workspace > Scripts > Utility > BeaTonic)

Montaggio ritmato a tempo di musica:
  1) Selezioni nel Media Pool le CLIP video (sorgenti) + la CANZONE (clip audio).
  2) "Analizza": ffmpeg rileva i cambi di scena nelle clip (le spezza in shot) e
     i BEAT della canzone.
  3) Scegli stile, durata, formato e ordine. Facoltativo: "Marker sui beat" crea una
     timeline con la canzone e un marker a ogni punto di stacco, da ascoltare e
     sistemare a mano (spostare, togliere, aggiungere col tasto M).
  4) "Monta": crea una timeline con la canzone e una shot tra un marker e l'altro
     (o, senza marker, direttamente sui beat).

Richiede ffmpeg (rilevato in automatico). Self-contained, zero dipendenze Python.
"""

import json
import math
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                      # Python < 3.7
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

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
def _reframes(thumbs, times, rf_ratio, rf_abs):
    """CAMBI D'INQUADRATURA dentro una ripresa continua (la camera si sposta su un altro
    soggetto, senza stacco): si confronta l'immagine MEDIA del secondo prima con quella
    del secondo dopo. La media smussa mani e persone che si muovono; normalizzando
    luminosita' e contrasto conta la struttura dell'immagine, non l'esposizione.
    Vale come cambio solo un picco che spicca sul movimento tipico di QUELLA clip.
    Ritorna [(tempo s, forza), ...]."""
    n = min(len(thumbs), len(times))
    if n < 20 or times[n - 1] <= times[0]:
        return []
    fps = (n - 1) / (times[n - 1] - times[0])
    sub = list(range(0, n, max(1, int(round(fps / 5.0)))))   # ~5 campioni al secondo

    def nmean(idx):
        m = [sum(thumbs[i][j] for i in idx) / float(len(idx)) for j in range(144)]
        mu = sum(m) / 144.0
        sd = (sum((v - mu) ** 2 for v in m) / 144.0) ** 0.5 or 1.0
        return [(v - mu) / sd for v in m]

    cv = [0.0] * len(sub)
    for q in range(5, len(sub) - 4):
        a, b = nmean(sub[q - 5:q]), nmean(sub[q:q + 5])
        cv[q] = 100.0 * sum(abs(x - y) for x, y in zip(a, b)) / 144.0
    pos = sorted(v for v in cv if v > 0)
    if not pos:
        return []
    med = pos[len(pos) // 2]
    out = []
    for q, v in enumerate(cv):
        if v >= rf_abs and v >= rf_ratio * med and v == max(cv[max(0, q - 7):q + 8]):
            out.append((times[sub[q]], v))
    return out


def scene_cuts(path, ratio=3.0, min_abs=0.06, rf_ratio=2.5, rf_abs=20.0, side=3):
    """Cambi di scena col metodo ADATTIVO (come l'AdaptiveDetector di PySceneDetect):
    ffmpeg da' il punteggio di differenza di OGNI fotogramma; e' uno stacco solo se
    quel punteggio SPICCA rispetto ai fotogrammi vicini (>= ratio volte la loro media).
    Cosi' una panoramica o un movimento veloce (punteggi alti ma continui) non tagliano,
    e uno stacco tra due inquadrature simili (punteggio basso ma isolato) si'.
    I FLASH vengono scartati: nella stessa passata ffmpeg da' anche una miniatura 16x9
    di ogni fotogramma; dopo uno stacco vero l'immagine RESTA diversa da prima, dopo un
    flash torna com'era. Le stesse miniature servono a trovare i cambi d'inquadratura.
    Ritorna ([(tempo, forza) stacchi], [(tempo, forza) cambi d'inquadratura])."""
    if not FFMPEG:
        return [], []
    meta = "_scene_meta.txt"            # relativo a _TMP: niente "C:" nel filtro
    fc = ("[0:v]scale=192:-2,split=2[a][b];"
          "[a]select='gte(scene,0)',metadata=print:file=%s[m];"
          "[b]scale=16:9,format=gray[g]" % meta)
    try:
        os.remove(os.path.join(_TMP, meta))
    except OSError:
        pass
    p = subprocess.run([FFMPEG, "-y", "-i", path, "-filter_complex", fc,
                        "-map", "[m]", "-f", "null", os.devnull,
                        "-map", "[g]", "-f", "rawvideo", "-"],
                       capture_output=True, cwd=_TMP, creationflags=_NOWIN)
    raw = p.stdout or b""
    thumbs = [raw[k:k + 144] for k in range(0, len(raw) - 143, 144)]
    try:
        with open(os.path.join(_TMP, meta), "r") as f:
            txt = f.read()
    except OSError:
        return [], []

    def diff(a, b):                     # differenza media tra due miniature (0..255)
        if a < 0 or b >= len(thumbs):
            return None
        x, y = thumbs[a], thumbs[b]
        return sum(abs(x[j] - y[j]) for j in range(144)) / 144.0

    times, scores = [], []
    for line in txt.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            times.append(float(m.group(1)))
            continue
        m = re.search(r"scene_score=([0-9.]+)", line)
        if m:
            scores.append(float(m.group(1)))
    n = min(len(times), len(scores))
    cand = []
    for i in range(1, n):
        s = scores[i]
        if s < min_abs:
            continue
        nb = scores[max(0, i - side):i] + scores[i + 1:min(n, i + 1 + side)]
        avg = sum(nb) / len(nb) if nb else 0.0
        if s >= ratio * max(avg, 0.005):
            cand.append(i)
    cuts = []
    for i in cand:
        jump = diff(i - 1, i)
        after = [d for d in (diff(i - 1, i + k) for k in (1, 2, 3, 4)) if d is not None]
        if jump and after and min(after) < 0.4 * jump:
            continue                        # torna com'era prima: flash, non stacco
        cuts.append((times[i], scores[i]))
    return cuts, _reframes(thumbs, times, rf_ratio, rf_abs)


def _decode_mono(path, sr=11025, af=None, name="_song.wav"):
    out = os.path.join(_TMP, name)
    cmd = [FFMPEG, "-y", "-i", path, "-ac", "1", "-ar", str(sr)]
    if af:
        cmd += ["-af", af]
    subprocess.run(cmd + [out], capture_output=True, creationflags=_NOWIN)
    return _read_wav(out)


def _wav_dur(p):
    try:
        w = wave.open(p, "rb")
        d = w.getnframes() / float(w.getframerate())
        w.close()
        return d
    except Exception:
        return 0.0


def _read_wav(p):
    w = wave.open(p, "rb")
    raw, sr = w.readframes(w.getnframes()), w.getframerate()
    w.close()
    data = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return [x / 32768.0 for x in data], sr


def _energy(sig, hop):
    return [sum(x * x for x in sig[i:i + hop]) for i in range(0, len(sig) - hop, hop)]


def _flux(E):
    """Forza dei colpi: aumento di energia in scala logaritmica (i colpi deboli
    contano, quelli fortissimi non schiacciano tutto il resto)."""
    m = (sum(E) / len(E)) if E else 1.0
    m = m or 1e-9
    L = [math.log(1.0 + 100.0 * e / m) for e in E]
    return [0.0] + [max(0.0, L[k] - L[k - 1]) for k in range(1, len(L))]


def _movavg(x, half):
    cs = [0.0]
    for v in x:
        cs.append(cs[-1] + v)
    n = len(x)
    return [(cs[min(n, i + half + 1)] - cs[max(0, i - half)]) /
            (min(n, i + half + 1) - max(0, i - half)) for i in range(n)]


def _tempo(o, fps):
    """Periodo del beat (in frame) per autocorrelazione dei colpi, preferendo il
    'beat che si sente' (~90-150 BPM): su un pezzo frenetico prende il mezzo tempo,
    non i sedicesimi del charleston."""
    import operator
    n = len(o)
    m = sum(o) / n
    x = [v - m for v in o]
    lo, hi = int(fps * 60 / 200), int(fps * 60 / 55)
    ac = {}
    for lag in range(max(1, lo - 1), min(hi + 1, n - 1) + 1):
        ac[lag] = sum(map(operator.mul, x[lag:], x[:-lag]))
    best, bl = None, lo
    for lag in range(lo, hi + 1):
        if lag not in ac:
            continue
        bpm = 60.0 * fps / lag
        w = math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.9) ** 2)
        if best is None or ac[lag] * w > best:
            best, bl = ac[lag] * w, lag
    bpm = 60.0 * fps / bl
    half = int(round(bl / 2.0)); dbl = bl * 2
    if bpm < 85 and half in ac and ac[half] > 0.5 * ac[bl]:
        bl = half                                   # troppo lento: raddoppia
    elif bpm > 165 and dbl in ac and ac[dbl] > 0.5 * ac[bl]:
        bl = dbl                                    # troppo veloce: dimezza
    a, b, c = ac.get(bl - 1), ac.get(bl), ac.get(bl + 1)
    if a is not None and c is not None and (a - 2 * b + c) != 0:
        return bl + 0.5 * (a - c) / (a - 2 * b + c)  # periodo frazionario
    return float(bl)


def _beat_track(o, P, tight=100.0):
    """Beat tracking a programmazione dinamica (Ellis 2007): sceglie la sequenza
    di beat che cade sui colpi forti restando il piu' regolare possibile intorno
    al periodo P (tollera piccoli cambi di tempo, ignora i colpi fuori griglia)."""
    n = len(o)
    lo, hi = max(1, int(round(P / 2))), int(round(2 * P))
    pen = [0.0] * (hi + 1)
    for d in range(1, hi + 1):
        pen[d] = -tight * math.log(d / P) ** 2
    thresh = 0.01 * (max(o) or 1.0)
    score, back = [0.0] * n, [-1] * n
    first = True
    for t in range(n):
        best, bp = None, -1
        for p in range(max(0, t - hi), t - lo + 1):
            v = score[p] + pen[t - p]
            if best is None or v > best:
                best, bp = v, p
        if t - hi < 0:                              # beat precedente "prima dell'inizio"
            dmin = max(lo, t + 1)
            if dmin <= hi:
                v0 = max(pen[d] for d in range(dmin, hi + 1))
                if best is None or v0 > best:
                    best, bp = v0, -1
        score[t] = o[t] + (best if best is not None else 0.0)
        if first and o[t] < thresh:
            back[t] = -1
        else:
            back[t] = bp
            first = False
    peaks = [score[t] for t in range(1, n - 1) if score[t - 1] <= score[t] >= score[t + 1]]
    if not peaks:
        return []
    med = sorted(peaks)[len(peaks) // 2]
    last = max(t for t in range(1, n - 1)
               if score[t - 1] <= score[t] >= score[t + 1] and score[t] >= 0.5 * med)
    beats, t = [], last
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = beats[::-1]
    # togli i beat deboli in testa e in coda (la catena li "inventa" oltre la musica)
    st = [max(o[max(0, b - 2):b + 3]) for b in beats]
    thr = 0.5 * (sum(v * v for v in st) / len(st)) ** 0.5 if st else 0.0
    a, z = 0, len(beats)
    while a < z and st[a] < thr:
        a += 1
    while z > a and st[z - 1] < thr:
        z -= 1
    return beats[a:z]


def beat_grid(path, hop=128):
    """'Ascolta' la canzone: trova il tempo, la griglia dei beat e le battute.
    Ritorna (bpm, [tempi beat s], [posizione nella battuta 0..3], [intensita' 0..1]).
    Dove la canzone tace o sfuma non mette beat (gate sull'energia)."""
    sig, sr = _decode_mono(path)
    low, _ = _decode_mono(path, af="lowpass=f=150,lowpass=f=150", name="_song_low.wav")
    E, EL = _energy(sig, hop), _energy(low, hop)
    n = min(len(E), len(EL))
    if n < 50:
        return 0.0, [], [], []
    fps = float(sr) / hop
    fb, fl = _flux(E[:n]), _flux(EL[:n])
    o = [b + l for b, l in zip(fb, fl)]             # la cassa (bassi) conta doppio
    base = _movavg(o, int(fps * 0.2))
    o = [max(0.0, v - m) for v, m in zip(o, base)]  # solo i colpi sopra la media locale
    sd = (sum(v * v for v in o) / n) ** 0.5 or 1.0
    o = [v / sd for v in o]
    o = [o[0]] + [0.25 * o[i - 1] + 0.5 * o[i] + 0.25 * o[i + 1]
                  for i in range(1, n - 1)] + [o[-1]]
    P = _tempo(o, fps)
    bf = _beat_track(o, P)
    if len(bf) < 4:
        return 0.0, [], [], []
    # battute: il "1" e' dove la cassa e' piu' forte (fase 0..3 con piu' bassi)
    lowstr = [max(fl[max(0, f - 2):f + 3]) for f in bf]
    phase = max(range(4), key=lambda k: sum(lowstr[k::4]) / max(1, len(lowstr[k::4])))
    bpos = [(i - phase) % 4 for i in range(len(bf))]
    # gate: niente beat nei silenzi / sfumati
    rms = _movavg([(e / hop) ** 0.5 for e in E[:n]], int(fps * 0.2))
    med = sorted(r for r in rms if r > 0)
    gate = 0.12 * (med[len(med) // 2] if med else 0.0)
    keep = [i for i, f in enumerate(bf) if rms[f] >= gate]
    # intensita' per beat: energia smussata ~1s, normalizzata tra 10 e 90 percentile
    sm = _movavg(rms, int(fps * 0.5))
    ev = [sm[bf[i]] for i in keep]
    if ev:
        srt = sorted(ev)
        lo_, hi_ = srt[int(0.1 * len(srt))], srt[min(len(srt) - 1, int(0.9 * len(srt)))]
        rng = (hi_ - lo_) or 1.0
        intens = [max(0.0, min(1.0, (e - lo_) / rng)) for e in ev]
    else:
        intens = []
    times = [round(bf[i] / fps, 3) for i in keep]
    return 60.0 * fps / P, times, [bpos[i] for i in keep], intens


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


MIN_SCENE = 0.6   # s: due stacchi piu' vicini di cosi' -> si tiene il piu' netto
MIN_TAKE = 3.0    # s: un cambio d'inquadratura crea pezzi lunghi almeno cosi'
SENS = {          # (stacco: quanto spicca, minimo | inquadratura: quanto spicca, minimo)
    "Media (consigliata)": (3.0, 0.06, 2.5, 20.0),
    "Alta (trova di più)": (2.2, 0.035, 2.0, 12.0),
    "Bassa (solo stacchi netti)": (4.5, 0.10, 3.0, 30.0),
    "Non dividere (clip intere)": None,
}


def build_shots(videos, params, set_log):
    """Spezza ogni clip ai cambi di scena e d'inquadratura -> (shot {clip,fps,in,out},
    n. stacchi, n. cambi d'inquadratura). Con params=None ogni clip resta intera."""
    shots, n_cut, n_ref = [], 0, 0
    for i, c in enumerate(videos, 1):
        path = c.GetClipProperty("File Path") or ""
        fps = _clip_fps(c)
        dur = _clip_dur_sec(c)
        if not params or not path:
            if dur > 0.1:
                shots.append({"clip": c, "fps": fps, "in": 0.0, "out": dur})
            continue
        set_log("Analisi scene %d/%d: %s…" % (i, len(videos), c.GetName()))
        cuts, refr = scene_cuts(path, *params)
        kept = []
        for t, s in cuts:
            if t < MIN_SCENE or dur - t < MIN_SCENE:
                continue
            if kept and t - kept[-1][0] < MIN_SCENE:
                if s > kept[-1][1]:
                    kept[-1] = (t, s)       # troppo vicini: vince lo stacco piu' netto
                continue
            kept.append((t, s))
        bounds = [0.0] + [t for t, _ in kept] + [dur]
        n_cut += len(kept)
        for t, v in sorted(refr, key=lambda x: -x[1]):    # i cambi piu' netti prima
            if all(abs(t - b) >= MIN_TAKE for b in bounds):
                bounds.append(t)
                n_ref += 1
        bounds.sort()
        for a, b in zip(bounds, bounds[1:]):
            shots.append({"clip": c, "fps": fps, "in": a, "out": b})
    return shots, n_cut, n_ref


# --------------------------------------------------------------------------
# Montaggio: una shot per stacco, stacchi esatti al fotogramma
# --------------------------------------------------------------------------
STYLES = {
    "Veloce (ogni beat)": "beat",
    "Medio (ogni 2 beat)": "half",
    "Lento (ogni battuta)": "bar",
    "Dinamico (segue l'energia)": "dyn",   # densita' per battuta guidata dall'energia
}
MIN_CUT = 0.33   # s: nessuno stacco piu' corto di cosi' (mai tagli ogni 2 fotogrammi)
FORMATS = {
    "Orizzontale 16:9": (1920, 1080),
    "Verticale 9:16": (1080, 1920),
}


def _cut_points(beats, bpos, style, intens):
    """Punti di stacco scelti SULLA GRIGLIA MUSICALE: beat, mezze battute o battute.
    'style' e' uno stile (STYLES) o una delle PROPOSTE (energia / groove / cinema)."""
    mode = STYLES.get(style, style)
    barv = [0.5] * len(beats)
    if mode in ("dyn", "energia", "groove", "cinema"):   # energia di ogni battuta
        i = 0
        while i < len(beats):
            j = i + 1
            while j < len(beats) and bpos[j] != 0:
                j += 1
            seg = intens[i:j]
            v = sum(seg) / len(seg) if seg else 0.5
            for k in range(i, j):
                barv[k] = v
            i = j
    pts, barno = [], 0
    for i, t in enumerate(beats):
        p, v = bpos[i], barv[i]
        barno += 1 if p == 0 else 0
        if mode == "beat":
            keep = True
        elif mode == "half":
            keep = p in (0, 2)
        elif mode == "bar":
            keep = p == 0
        elif mode == "energia":       # serrato: ogni beat appena la canzone spinge
            keep = True if v >= 0.45 else (p in (0, 2) if v >= 0.15 else p == 0)
        elif mode == "groove":        # sull'1 e sul 3, ogni beat solo nei punti piu' forti
            keep = p in (0, 2) or v >= 0.8
        elif mode == "cinema":        # una battuta per inquadratura, due nelle parti calme
            keep = p == 0 and (v >= 0.4 or barno % 2 == 1)
        else:                         # dinamico: fitto se spinge, largo se calmo
            keep = True if v >= 0.6 else (p in (0, 2) if v >= 0.33 else p == 0)
        if keep:
            pts.append(t)
    return pts


def _intervals(beats, bpos, style, dur, intens, song_len=0.0):
    """Intervalli (inizio, fine) del montaggio: partono da 0 (come la canzone in
    timeline), mai piu' corti di MIN_CUT, e finiscono sull'inizio di una battuta.
    Se il montaggio copre TUTTA la canzone (es. adattata alla durata) finisce con lei."""
    pts, last = [], 0.0
    for t in _cut_points(beats, bpos, style, intens):
        if t - last >= MIN_CUT and t <= dur + 0.01:
            pts.append(t)
            last = t
    if song_len and dur >= song_len - 0.05:
        if not pts or song_len - pts[-1] >= MIN_CUT:
            pts.append(song_len)
        bounds = [0.0] + pts
        return list(zip(bounds, bounds[1:]))
    if not pts:
        return []
    downs = set(t for t, p in zip(beats, bpos) if p == 0)
    for k in range(len(pts) - 1, max(-1, len(pts) - 9), -1):
        if pts[k] in downs:           # chiudi sul "1" piu' vicino alla durata scelta
            pts = pts[:k + 1]
            break
    bounds = [0.0] + pts
    return list(zip(bounds, bounds[1:]))


PRESETS = [   # (chiave, nome, descrizione, ordine delle clip): tre montaggi diversi tra loro
    ("energia", "Energia", "Un taglio a ogni beat dove la canzone spinge, a mezza battuta altrove: "
     "ritmo serrato, clip mischiate.", "Ord: Casuale (mischia tutto)"),
    ("groove", "Groove", "Tagli sull'1 e sul 3 della battuta, ogni beat solo nei punti più forti: "
     "segue il ballo, clip in sequenza.", "Ord: Nome A→Z"),
    ("cinema", "Cinematico", "Un'inquadratura per battuta, due battute nelle parti calme: "
     "lascia respirare le immagini.", "Ord: Selezione"),
]

ORDERS = {
    "Ord: Nome A→Z": "nameaz",
    "Ord: Nome Z→A": "nameza",
    "Ord: Selezione": "selezione",
    "Ord: Casuale (mischia tutto)": "casuale",
}


def order_shots(shots, mode, seed=None):
    """Ordine delle shot nel montaggio. Per nome: le shot dello stesso video restano
    insieme e in ordine di tempo. Casuale: mischia le shot di TUTTI i video (con lo
    stesso 'seed' esce lo stesso ordine: l'anteprima dell'editor = il montaggio)."""
    tape = list(shots)
    if mode == "casuale":
        import random
        random.Random(seed).shuffle(tape)
    elif mode in ("nameaz", "nameza"):
        tape.sort(key=lambda s: s["in"])                   # sort stabili: prima il tempo,
        tape.sort(key=lambda s: (s["clip"].GetName() or "").lower(),
                  reverse=(mode == "nameza"))              # poi il nome della clip
    return tape                                            # "selezione": come selezionate


def _new_timeline(mp, base):
    new = mp.CreateEmptyTimeline(base)
    n = 2
    while new is None and n < 100:
        new = mp.CreateEmptyTimeline("%s_%d" % (base, n)); n += 1
    return new


def _put_song(mp, song, ns, frames=None):
    """Canzone su A1 dall'inizio della timeline: lunga 'frames', o intera se None."""
    info = {"mediaPoolItem": song, "recordFrame": ns, "trackIndex": 1, "mediaType": 2}
    if frames:
        info.update({"startFrame": 0, "endFrame": int(frames)})
    try:
        mp.AppendToTimeline([info])
    except Exception:
        pass


# --------------------------------------------------------------------------
# Finestre a parte (Chrome/Edge in modalita' "app", oppure il browser predefinito)
# servite da un piccolo server DENTRO il plugin, SOLO su questo computer.
# Disegno fluido, trascinamento, zoom, scrub audio al passaggio del mouse, marker
# trascinabili. I marker si salvano da soli in markers.json; "Monta" li legge.
# --------------------------------------------------------------------------
WEB = os.path.join(_TMP, "editor")
SESSION = [""]            # cambia a ogni Analizza: i marker vecchi non valgono piu'
BROWSER_AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")

EDITOR_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>BeaTonic · Marker</title>
<link rel="icon" id="fav" href="/logo">
<style>
:root {
  --bg: #0f1115; --panel: #111827; --ruler: #0b0d11; --line: #1f2937; --fg: #e5e7eb;
  --muted: #9ca3af; --accent: #7c3aed; --accent2: #a78bfa; --wave: #7467d6; --beat: #171b27;
  --bar: #272e44; --mark: #a78bfa; --sel: #ffffff; --head: #f59e0b; --hover: #ffffff;
  --dim: rgba(0, 0, 0, .45); --btn: #1f2937; --btnh: #374151; --onfg: #ffffff;
  --font: "Segoe UI", Inter, system-ui, sans-serif;
}
body.labs {
  --bg: #F3F0F7; --panel: #FBF4FF; --ruler: #FBF4FF; --line: #0E0418; --fg: #0E0418;
  --muted: #4B4458; --accent: #EE3A5B; --accent2: #B0126E; --wave: #A58FC4; --beat: #ECE3F5;
  --bar: #D8C8EA; --mark: #EE3A5B; --sel: #0E0418; --head: #F7A541; --hover: #0E0418;
  --dim: rgba(14, 4, 24, .08); --btn: #FBF4FF; --btnh: #EDE3F7; --onfg: #0E0418;
  --font: "Public Sans", "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg);
  font: 13px var(--font); overflow: hidden; user-select: none; -webkit-user-select: none; }
#app { display: flex; flex-direction: column; height: 100%; }
#top { display: flex; align-items: center; gap: 6px; padding: 8px 12px; background: var(--panel);
  border-bottom: 1px solid var(--line); flex-wrap: wrap; }
#logo { height: 30px; }
#title { font-weight: 800; font-size: 16px; color: var(--accent2); }
#song { color: var(--muted); margin-right: auto; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 26vw; padding-left: 6px; }
button, select, input { font: inherit; color: var(--fg); background: var(--btn);
  border: 1px solid var(--line); border-radius: 7px; padding: 6px 10px; outline: none; }
body.labs button, body.labs select, body.labs input { border-width: 2px; }
button { cursor: pointer; white-space: nowrap; }
button:hover { background: var(--btnh); }
button.on { background: var(--accent); border-color: var(--accent); color: var(--onfg); }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--onfg); font-weight: 700; }
input[type=number] { width: 62px; }
.sep { width: 1px; height: 24px; background: var(--line); margin: 0 3px; opacity: .6; }
#ov { display: block; width: 100%; height: 58px; cursor: pointer; border-bottom: 1px solid var(--line); }
#mainwrap { flex: 1; display: flex; min-height: 120px; }
#cv { flex: 1; position: relative; min-width: 0; }
#main { position: absolute; left: 0; top: 0; width: 100%; height: 100%; display: block; cursor: grab; }
#prev { width: clamp(220px, 27vw, 440px); display: flex; flex-direction: column; gap: 8px; padding: 10px;
  background: var(--panel); border-left: 1px solid var(--line); }
#prev[hidden] { display: none; }
#prevHead { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
#prevHead b { color: var(--accent2); }
#prevBox { flex: 1; position: relative; min-height: 0; }
#pv { position: absolute; left: 0; top: 0; width: 100%; height: 100%; display: block; }
#prevInfo { color: var(--muted); min-height: 2.6em; line-height: 1.3; font-variant-numeric: tabular-nums; }
#status { display: flex; gap: 16px; padding: 6px 12px; background: var(--panel);
  border-top: 1px solid var(--line); color: var(--muted); flex-wrap: wrap; align-items: center; }
#status b { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }
#hint { margin-left: auto; opacity: .85; }
.overlay { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, .55); z-index: 10; }
.overlay[hidden] { display: none; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 22px 26px; max-width: 680px; line-height: 1.55; }
.card h2 { margin: 0 0 10px; color: var(--accent2); }
.card .row { display: flex; gap: 8px; margin-top: 14px; justify-content: flex-end; }
kbd { border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 4px; padding: 0 5px;
  font: 12px ui-monospace, Consolas, monospace; background: var(--ruler); }
td { padding: 3px 14px 3px 0; vertical-align: top; }
</style>
</head>
<body>
<div id="app">
  <div id="top">
    <img id="logo" alt="" hidden>
    <span id="title">BeaTonic · Marker</span>
    <span id="song"></span>
    <button id="bPlay" title="Play / pausa (Spazio)">▶ Play</button>
    <div class="sep"></div>
    <button id="bAdd" title="Marker sul cursore arancione. Più veloce: Q o M mettono il marker dove c'è il mouse">+ Marker</button>
    <button id="bDel" title="Togli il marker selezionato (Canc)">− Marker</button>
    <button id="bSnap" class="on" title="ACCESO: un marker messo o spostato vicino a un beat ci si attacca da solo, così il taglio cade a tempo. Spento: va esattamente dove lo metti. (N; tieni Alt mentre trascini per sganciarlo)">Aggancia ai beat</button>
    <button id="bScrub" class="on" title="ACCESO: passando il mouse sulla forma d'onda senti l'audio in quel punto, come trascinare la testina in Resolve. (S)">Ascolto col mouse</button>
    <button id="bPrev" class="on" title="Mostra / nascondi l'anteprima del montato (P)">Anteprima</button>
    <div class="sep"></div>
    <button id="bUndo" title="Annulla (Ctrl+Z)">↶</button>
    <button id="bRedo" title="Ripeti (Ctrl+Shift+Z)">↷</button>
    <div class="sep"></div>
    <select id="selStyle" title="Stile: cambiandolo i marker si rifanno subito (Ctrl+Z per tornare indietro)"></select>
    <input id="inDur" type="number" min="1" step="1" title="Durata del montaggio (secondi): cambiandola i marker si rifanno subito"><span>s</span>
    <button id="bRegen" title="Rifai i marker dai beat con questo stile e durata (cancella le modifiche a mano; Ctrl+Z le riporta)">↺ Rifai</button>
    <button id="bClear" title="Togli tutti i marker">Svuota</button>
    <div class="sep"></div>
    <button id="bFit" title="Tutta la canzone (0)">Tutta</button>
    <button id="bFull" title="Schermo intero (F)">⛶</button>
    <button id="bHelp" title="Comandi (H)">?</button>
    <button id="bDone" class="primary" title="Salva e torna a DaVinci Resolve">✓ Fatto</button>
  </div>
  <canvas id="ov"></canvas>
  <div id="mainwrap">
    <div id="cv"><canvas id="main"></canvas></div>
    <aside id="prev">
      <div id="prevHead"><b>Anteprima del montato</b>
        <select id="prevMode" title="Clip: le clip vere che verranno montate. Bianco/nero: si alterna a ogni cambio clip">
          <option value="bn" selected>Bianco / nero</option><option value="clip">Clip</option></select></div>
      <div id="prevBox"><canvas id="pv"></canvas></div>
      <div id="prevInfo">—</div>
    </aside>
  </div>
  <div id="status">
    <span>Marker <b id="stMarks">0</b></span>
    <span>Stacco più corto <b id="stShort">—</b></span>
    <span>Fine montaggio <b id="stEnd">—</b></span>
    <span>Cursore <b id="stHead">0:00:00</b></span>
    <span>Selezionato <b id="stSel">—</b></span>
    <span>Vista <b id="stZoom">—</b></span>
    <span id="saved"></span>
    <span id="hint">Q / M = marker dove c'è il mouse · Trascina = scorri · Rotella = zoom · Trascina un marker = spostalo · Clic destro = toglilo</span>
  </div>
</div>

<div id="start" class="overlay"><div class="card"><h2>BeaTonic · Marker</h2><div id="startMsg">Preparo l'editor…</div></div></div>

<div id="help" class="overlay" hidden><div class="card">
  <h2>Comandi</h2>
  <p>Ogni marker è un <b>cambio clip</b>. L'ultimo marker è la <b>fine</b> del montaggio.</p>
  <table>
    <tr><td><b>Mouse</b></td><td></td></tr>
    <tr><td>Trascina sull'onda</td><td>scorre la timeline</td></tr>
    <tr><td>Rotella</td><td>zoom attorno al mouse (con <kbd>Shift</kbd>: scorre)</td></tr>
    <tr><td>Mouse sopra l'onda</td><td>ascolti quel punto (Scrub)</td></tr>
    <tr><td>Clic</td><td>sposta il cursore</td></tr>
    <tr><td>Doppio clic</td><td>nuovo marker</td></tr>
    <tr><td>Trascina un marker</td><td>lo sposti (si aggancia ai beat; <kbd>Alt</kbd> = libero)</td></tr>
    <tr><td>Clic destro su un marker</td><td>lo togli</td></tr>
    <tr><td>Striscia in alto</td><td>clic/trascina per andare in un punto della canzone</td></tr>
    <tr><td><b>Tastiera</b></td><td></td></tr>
    <tr><td><kbd>Spazio</kbd></td><td>play / pausa</td></tr>
    <tr><td><kbd>Q</kbd> o <kbd>M</kbd></td><td>marker <b>dove c'è il mouse</b>, senza cliccare (mentre suona: dove sta suonando, battilo a tempo)</td></tr>
    <tr><td><kbd>P</kbd></td><td>anteprima del montato sì / no</td></tr>
    <tr><td><kbd>Canc</kbd></td><td>togli il marker selezionato</td></tr>
    <tr><td><kbd>←</kbd> <kbd>→</kbd></td><td>sposta il marker selezionato di 1 fotogramma (<kbd>Shift</kbd>: 10) o il cursore</td></tr>
    <tr><td><kbd>↑</kbd> <kbd>↓</kbd></td><td>marker precedente / successivo</td></tr>
    <tr><td><kbd>+</kbd> <kbd>−</kbd> <kbd>0</kbd></td><td>zoom avanti / indietro / tutta la canzone</td></tr>
    <tr><td><kbd>Ctrl+Z</kbd> <kbd>Ctrl+Shift+Z</kbd></td><td>annulla / ripeti</td></tr>
    <tr><td><kbd>S</kbd> <kbd>N</kbd> <kbd>F</kbd></td><td>scrub · aggancio beat · schermo intero</td></tr>
  </table>
  <div class="row"><button id="bHelpClose" class="primary">Ok</button></div>
</div></div>

<div id="doneBox" class="overlay" hidden><div class="card">
  <h2>Fatto!</h2>
  <p><b id="doneN">0</b> marker salvati.</p>
  <p>Torna su <b>DaVinci Resolve</b> e premi <b>▶ Monta</b> in BeaTonic: metterà una clip tra un marker e l'altro.</p>
  <div class="row"><button id="bKeep">Continua a modificare</button><button id="bClose" class="primary">Chiudi finestra</button></div>
</div></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const main = $("main"), ov = $("ov"), mg = main.getContext("2d"), og = ov.getContext("2d");
const pv = $("pv"), pg = pv.getContext("2d");
let PW = 0, PH = 0, plan = [], sprites = [], prevOn = true, lastPiece = -1, flashUntil = 0;
let S = null, ctx = null, buf = null, DUR = 0, SR = 44100, mono = null, LV = [], GAIN = 1;
let marks = [], sel = -1, hoverMark = -1, undo = [], redo = [];
let t0 = 0, spp = 0.01, head = 0, hoverT = null;
let snap = true, scrub = true, follow = true, play = null, drag = null, ovDrag = null;
let W = 0, H = 0, OW = 0, OH = 0, DPR = 1, dirty = true, ovImg = null, font = "sans-serif";
const RULER = 28, col = {};

// ---------------------------------------------------------------- utilita'
function readColors() {
  const cs = getComputedStyle(document.body);
  for (const k of ["bg", "ruler", "line", "fg", "muted", "wave", "beat", "bar", "mark", "sel",
                   "head", "hover", "dim", "accent"])
    col[k] = cs.getPropertyValue("--" + k).trim();
  font = cs.getPropertyValue("--font").trim() || "sans-serif";
}
function bsearch(a, t) {                    // primo indice con a[i] >= t
  let lo = 0, hi = a.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < t) lo = m + 1; else hi = m; }
  return lo;
}
function nearestIn(a, t) {
  if (!a.length) return -1;
  const i = bsearch(a, t);
  if (i <= 0) return 0;
  if (i >= a.length) return a.length - 1;
  return (t - a[i - 1] <= a[i] - t) ? i - 1 : i;
}
function fmt(t) {
  t = Math.max(0, t);
  const fps = S ? S.fps : 25, m = Math.floor(t / 60), s = Math.floor(t % 60);
  const f = Math.min(Math.round(fps) - 1, Math.floor((t % 1) * fps + 1e-6));
  return m + ":" + String(s).padStart(2, "0") + ":" + String(f).padStart(2, "0");
}
const X = t => (t - t0) / spp;
const T = x => t0 + x * spp;
function clampView() {
  const maxSpp = Math.max(DUR / Math.max(W, 1), 0.0005);
  spp = Math.min(Math.max(spp, 0.00005), maxSpp);
  t0 = Math.min(Math.max(t0, 0), Math.max(0, DUR - W * spp));
}
function ensureAudio() { if (ctx && ctx.state === "suspended") ctx.resume(); }

// ---------------------------------------------------------------- forma d'onda
function buildPeaks() {
  const n = buf.length, ch = buf.numberOfChannels;
  mono = new Float32Array(n);
  for (let c = 0; c < ch; c++) {
    const d = buf.getChannelData(c);
    for (let i = 0; i < n; i++) mono[i] += d[i] / ch;
  }
  LV = [];
  const m = Math.ceil(n / 32), mn = new Float32Array(m), mx = new Float32Array(m);
  for (let b = 0; b < m; b++) {
    let lo = 1, hi = -1;
    const e = Math.min(n, (b + 1) * 32);
    for (let i = b * 32; i < e; i++) { const v = mono[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    mn[b] = lo; mx[b] = hi;
  }
  LV.push({block: 32, min: mn, max: mx});
  while (LV[LV.length - 1].min.length > 1024) {        // livelli piu' grossi (x8)
    const p = LV[LV.length - 1], k = Math.ceil(p.min.length / 8);
    const a = new Float32Array(k), z = new Float32Array(k);
    for (let b = 0; b < k; b++) {
      let lo = 1, hi = -1;
      const e = Math.min(p.min.length, (b + 1) * 8);
      for (let i = b * 8; i < e; i++) { if (p.min[i] < lo) lo = p.min[i]; if (p.max[i] > hi) hi = p.max[i]; }
      a[b] = lo; z[b] = hi;
    }
    LV.push({block: p.block * 8, min: a, max: z});
  }
  let peak = 0;
  const top = LV[LV.length - 1];
  for (let i = 0; i < top.min.length; i++) peak = Math.max(peak, -top.min[i], top.max[i]);
  GAIN = peak > 0 ? 0.95 / peak : 1;
}
function colRange(s0, s1) {                 // [min, max] dei campioni tra s0 e s1
  if (s1 - s0 <= 32) {
    let lo = 1, hi = -1;
    const a = Math.max(0, Math.floor(s0)), e = Math.min(mono.length, Math.ceil(s1) + 1);
    for (let i = a; i < e; i++) { const v = mono[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    return lo > hi ? null : [lo, hi];
  }
  let L = LV[0];
  for (const l of LV) if (l.block <= s1 - s0) L = l;
  const a = Math.max(0, Math.floor(s0 / L.block)), e = Math.min(L.min.length, Math.ceil(s1 / L.block));
  let lo = 1, hi = -1;
  for (let i = a; i < e; i++) { if (L.min[i] < lo) lo = L.min[i]; if (L.max[i] > hi) hi = L.max[i]; }
  return lo > hi ? null : [lo, hi];
}

// ---------------------------------------------------------------- disegno
function renderOvImg() {
  ovImg = document.createElement("canvas");
  ovImg.width = OW * DPR; ovImg.height = OH * DPR;
  const g = ovImg.getContext("2d");
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.fillStyle = col.ruler; g.fillRect(0, 0, OW, OH);
  g.fillStyle = col.wave;
  const mid = OH / 2, amp = OH / 2 - 3;
  for (let x = 0; x < OW; x++) {
    const r = colRange(x / OW * DUR * SR, (x + 1) / OW * DUR * SR);
    if (!r) continue;
    const y1 = mid - r[1] * GAIN * amp, y2 = mid - r[0] * GAIN * amp;
    g.fillRect(x, y1, 1, Math.max(1, y2 - y1));
  }
}
function drawOv() {
  og.setTransform(DPR, 0, 0, DPR, 0, 0);
  if (ovImg) og.drawImage(ovImg, 0, 0, OW, OH);
  const k = OW / DUR;
  og.fillStyle = col.mark;
  for (const m of marks) og.fillRect(Math.round(m * k), 0, 1, Math.round(OH * 0.35));
  const x0 = t0 * k, w = Math.max(3, W * spp * k);
  og.globalAlpha = 0.16; og.fillStyle = col.hover; og.fillRect(x0, 0, w, OH); og.globalAlpha = 1;
  og.strokeStyle = col.accent; og.lineWidth = 1.5; og.strokeRect(x0 + 0.75, 0.75, w - 1.5, OH - 1.5);
  og.fillStyle = col.head; og.fillRect(Math.round(curTime() * k) - 1, 0, 2, OH);
}
function drawMain() {
  const g = mg;
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.fillStyle = col.bg; g.fillRect(0, 0, W, H);
  const top = RULER, wh = H - RULER, mid = top + wh / 2, amp = wh / 2 - 10, vs = W * spp;
  // griglia: beat chiari, battute ("1") piu' marcate
  const b = S.beats, bp = S.bpos, i0 = bsearch(b, t0), i1 = bsearch(b, t0 + vs);
  const beatPx = (60 / (S.bpm || 120)) / spp;
  for (let i = i0; i < i1; i++) {
    const bar = bp[i] === 0;
    if (!bar && beatPx < 6) continue;
    if (bar && beatPx * 4 < 6) continue;
    g.fillStyle = bar ? col.bar : col.beat;
    g.fillRect(Math.round(X(b[i])), top, bar ? 2 : 1, wh);
  }
  // forma d'onda
  g.fillStyle = col.wave;
  if (spp * SR < 2) {                       // zoom estremo: la curva dei campioni
    g.strokeStyle = col.wave; g.lineWidth = 1.5; g.beginPath();
    const a = Math.max(0, Math.floor(t0 * SR) - 1), e = Math.min(mono.length, Math.ceil((t0 + vs) * SR) + 2);
    for (let i = a; i < e; i++) {
      const x = X(i / SR), y = mid - mono[i] * GAIN * amp;
      if (i === a) g.moveTo(x, y); else g.lineTo(x, y);
    }
    g.stroke();
  } else {
    for (let x = 0; x < W; x++) {
      const r = colRange(T(x) * SR, T(x + 1) * SR);
      if (!r) continue;
      const y1 = mid - r[1] * GAIN * amp, y2 = mid - r[0] * GAIN * amp;
      g.fillRect(x, y1, 1, Math.max(1, y2 - y1));
    }
  }
  g.globalAlpha = 0.35; g.fillStyle = col.line; g.fillRect(0, Math.round(mid), W, 1); g.globalAlpha = 1;
  // dopo l'ultimo marker: fuori dal montaggio
  if (marks.length) {
    const xe = Math.max(0, X(marks[marks.length - 1]));
    if (xe < W) { g.fillStyle = col.dim; g.fillRect(xe, top, W - xe, wh); }
  }
  // marker
  for (let i = 0; i < marks.length; i++) {
    const x = Math.round(X(marks[i]));
    if (x < -12 || x > W + 12) continue;
    const big = i === sel || i === hoverMark;
    g.fillStyle = i === sel ? col.sel : col.mark;
    g.fillRect(x - (big ? 1 : 0), top, big ? 3 : 2, wh);
  }
  // cursore del mouse
  if (hoverT !== null && !drag) {
    g.globalAlpha = 0.35; g.fillStyle = col.hover; g.fillRect(Math.round(X(hoverT)), top, 1, wh); g.globalAlpha = 1;
  }
  // righello col timecode
  g.fillStyle = col.ruler; g.fillRect(0, 0, W, RULER);
  g.fillStyle = col.line; g.globalAlpha = 0.6; g.fillRect(0, RULER - 1, W, 1); g.globalAlpha = 1;
  const fps = S.fps || 25;
  const steps = [1 / fps, 2 / fps, 5 / fps, 10 / fps, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  let step = steps[steps.length - 1];
  for (const s of steps) if (s / spp >= 90) { step = s; break; }
  g.font = "11px " + font; g.textBaseline = "top";
  for (let k = Math.ceil(t0 / step); k * step <= t0 + vs; k++) {
    const x = Math.round(X(k * step));
    g.fillStyle = col.muted; g.fillRect(x, RULER - 9, 1, 8);
    g.fillText(fmt(k * step), x + 4, 4);
  }
  // maniglie dei marker nel righello (si prendono meglio)
  for (let i = 0; i < marks.length; i++) {
    const x = Math.round(X(marks[i]));
    if (x < -12 || x > W + 12) continue;
    g.fillStyle = i === sel ? col.sel : col.mark;
    g.beginPath(); g.moveTo(x - 6, RULER - 13); g.lineTo(x + 7, RULER - 13); g.lineTo(x + 0.5, RULER); g.closePath(); g.fill();
  }
  // cursore di riproduzione
  const hx = Math.round(X(curTime()));
  g.fillStyle = col.head; g.fillRect(hx - 1, 0, 2, H);
  g.beginPath(); g.moveTo(hx - 7, 0); g.lineTo(hx + 7, 0); g.lineTo(hx, 9); g.closePath(); g.fill();
}

// ---------------------------------------------------------------- audio
function curTime() { return play ? Math.min(DUR, play.t + (ctx.currentTime - play.c0)) : head; }
function startPlay(from) {
  stopPlay();
  if (!ctx || !buf) return;
  ensureAudio();
  from = Math.max(0, Math.min(from, DUR - 0.01));
  const src = ctx.createBufferSource();
  src.buffer = buf; src.connect(ctx.destination); src.start(0, from);
  play = {src: src, c0: ctx.currentTime, t: from};
  src.onended = () => { if (play && play.src === src) { head = curTime(); play = null; updButtons(); dirty = true; } };
  follow = true; updButtons();
}
function stopPlay() {
  if (play) {
    const t = curTime();
    play.src.onended = null;
    try { play.src.stop(); } catch (e) {}
    play = null; head = t;
  }
  updButtons(); updClock();
}
function togglePlay() { if (play) stopPlay(); else startPlay(head); dirty = true; }
let lastScrub = 0, lastScrubT = -1;
function scrubAt(t) {                       // un granello di ~90 ms nel punto del mouse
  if (!scrub || !ctx || !buf || ctx.state !== "running") return;
  const now = performance.now();
  if (now - lastScrub < 40 || Math.abs(t - lastScrubT) < 0.004) return;
  lastScrub = now; lastScrubT = t;
  const src = ctx.createBufferSource(), gn = ctx.createGain(), c = ctx.currentTime, d = 0.09;
  src.buffer = buf; src.connect(gn); gn.connect(ctx.destination);
  gn.gain.setValueAtTime(0, c);
  gn.gain.linearRampToValueAtTime(0.9, c + 0.006);
  gn.gain.setValueAtTime(0.9, c + d - 0.02);
  gn.gain.linearRampToValueAtTime(0, c + d);
  src.start(c, Math.max(0, Math.min(t, DUR - 0.1)), d);
}

// ---------------------------------------------------------------- marker
function pushUndo() { undo.push(marks.slice()); if (undo.length > 300) undo.shift(); redo.length = 0; }
function dedupe() {                          // niente due marker nello stesso fotogramma
  const fr = 0.5 / (S.fps || 25), out = [];
  for (const t of marks) if (!out.length || t - out[out.length - 1] > fr) out.push(t);
  if (out.length !== marks.length) {
    const st = sel >= 0 ? marks[sel] : null;
    marks = out; sel = st === null ? -1 : nearestIn(marks, st);
  }
}
function changed() { buildPlan(); dirty = true; updStatus(); scheduleSave(); }
function commit(fn) { pushUndo(); fn(); marks.sort((a, b) => a - b); dedupe(); changed(); }
function snapT(t, px) {                      // aggancio al beat (entro px pixel, o 0.12 s)
  if (!snap || !S.beats.length) return t;
  const j = nearestIn(S.beats, t), bt = S.beats[j];
  const ok = px ? Math.abs(X(bt) - X(t)) <= px : Math.abs(bt - t) <= 0.12;
  return ok ? bt : t;
}
function markHere() {                       // Q / M: dove c'e' il mouse (o dove suona)
  if (play) addMark(curTime(), true);
  else if (hoverT !== null) addMark(hoverT, true, 8);
  else addMark(head, true);
}
function addMark(t, useSnap, px) {
  t = Math.min(Math.max(useSnap ? snapT(t, px || 0) : t, 0.001), DUR);
  const fr = 0.5 / (S.fps || 25);
  const j = nearestIn(marks, t);
  if (j >= 0 && Math.abs(marks[j] - t) < fr) { sel = j; dirty = true; updStatus(); return; }
  commit(() => marks.push(t));
  sel = marks.indexOf(t); updStatus();
}
function delMark(i) { if (i < 0 || i >= marks.length) return; commit(() => marks.splice(i, 1)); sel = -1; updStatus(); }
function nudgeSel(d) {
  if (sel < 0) return;
  const t = Math.min(Math.max(marks[sel] + d, 0.001), DUR), i = sel;
  commit(() => { marks[i] = t; });
  sel = nearestIn(marks, t); head = t; ensureVisible(t); updStatus(); updClock();
}
function jumpMark(dir) {
  const c = curTime();
  const i = dir < 0 ? bsearch(marks, c - 1e-4) - 1 : bsearch(marks, c + 1e-4);
  if (i >= 0 && i < marks.length) { sel = i; setHead(marks[i]); updStatus(); }
}
function doUndo() { if (!undo.length) return; redo.push(marks.slice()); marks = undo.pop(); sel = -1; changed(); }
function doRedo() { if (!redo.length) return; undo.push(marks.slice()); marks = redo.pop(); sel = -1; changed(); }
function cutPoints(mode) {                   // come BeaTonic: punti di stacco sulla griglia
  const b = S.beats, p = S.bpos, it = S.intens || [], barv = new Array(b.length).fill(0.5);
  if (["dyn", "energia", "groove", "cinema"].includes(mode)) {
    let i = 0;
    while (i < b.length) {
      let j = i + 1;
      while (j < b.length && p[j] !== 0) j++;
      const seg = it.slice(i, j), v = seg.length ? seg.reduce((a, c) => a + c, 0) / seg.length : 0.5;
      for (let k = i; k < j; k++) barv[k] = v;
      i = j;
    }
  }
  const out = [];
  let barno = 0;
  for (let i = 0; i < b.length; i++) {
    const q = p[i], v = barv[i];
    if (q === 0) barno++;
    let keep;
    if (mode === "beat") keep = true;
    else if (mode === "half") keep = q === 0 || q === 2;
    else if (mode === "bar") keep = q === 0;
    else if (mode === "energia") keep = v >= 0.45 ? true : (v >= 0.15 ? (q === 0 || q === 2) : q === 0);
    else if (mode === "groove") keep = q === 0 || q === 2 || v >= 0.8;
    else if (mode === "cinema") keep = q === 0 && (v >= 0.4 || barno % 2 === 1);
    else keep = v >= 0.6 ? true : (v >= 0.33 ? (q === 0 || q === 2) : q === 0);
    if (keep) out.push(b[i]);
  }
  return out;
}
function fromBeats(mode, dur) {
  const pts = [];
  let last = 0;
  for (const t of cutPoints(mode)) if (t - last >= S.min_cut && t <= dur + 0.01) { pts.push(t); last = t; }
  const downs = new Set();
  S.beats.forEach((t, i) => { if (S.bpos[i] === 0) downs.add(t); });
  if (dur >= DUR - 0.05) {                    // il montaggio copre tutta la canzone
    if (!pts.length || DUR - pts[pts.length - 1] >= S.min_cut) pts.push(DUR);   // (es. adattata)
    return pts;
  }
  for (let k = pts.length - 1; k >= Math.max(0, pts.length - 8); k--)
    if (downs.has(pts[k])) return pts.slice(0, k + 1);
  return pts;
}

// ---------------------------------------------------------------- anteprima del montato
// Stesse regole di BeaTonic: una shot NUOVA a ogni stacco, ogni ritorno a una clip
// ne mostra un pezzo nuovo, si ripete solo quando il materiale e' finito.
function buildPlan() {
  plan = [];
  const tape = S.tape || [];
  if (!tape.length || !marks.length) return;
  const rp = new Map();
  let si = 0, giri = 0, prev = 0;
  const rem = k => tape[k].out - tape[k].in - (rp.get(k) || 0);
  const take = (k, amt, t) => {
    const st = tape[k].in + (rp.get(k) || 0);
    rp.set(k, st + amt - tape[k].in);
    plan.push({t0: t, t1: t + amt, c: tape[k].c, s0: st, rep: giri > 0});
  };
  const whole = need => {
    for (let j = 0; j < tape.length; j++) { const k = (si + j) % tape.length; if (rem(k) >= need) return k; }
    return -1;
  };
  for (const m of marks) {
    const len = m - prev, t = prev;
    prev = m;
    if (len <= 0.02) continue;
    let k = whole(len);
    if (k < 0 && S.reuse) { rp.clear(); giri++; k = whole(len); }
    if (k >= 0) { si = k + 1; take(k, len, t); continue; }
    let longest = 0;
    for (let j = 0; j < tape.length; j++) longest = Math.max(longest, rem(j));
    const n = longest > 0.02 ? Math.ceil(len / longest - 1e-9) : 0;
    if (!n || len / n < S.min_cut) break;
    let ok = true;
    for (let q = 0; q < n; q++) {
      const kk = whole(len / n);
      if (kk < 0) { ok = false; break; }
      si = kk + 1; take(kk, len / n, t + q * len / n);
    }
    if (!ok) break;
  }
}
function pieceAt(t) {
  let lo = 0, hi = plan.length - 1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (t < plan[m].t0) hi = m - 1; else if (t >= plan[m].t1) lo = m + 1; else return m;
  }
  return -1;
}
function loadSprite(c) {
  if (sprites[c]) return sprites[c];
  const im = new Image(), sp = {img: im, ok: false, err: false};
  sprites[c] = sp;
  im.onload = () => { sp.ok = true; dirty = true; };
  im.onerror = () => { sp.err = true; dirty = true; };
  im.src = "/thumb/" + c + "?s=" + encodeURIComponent(S.session);
  return sp;
}
function previewTime() { return play ? curTime() : (hoverT !== null ? hoverT : head); }
function drawPreview() {
  if (!prevOn || !PW) return;
  const g = pg;
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.fillStyle = col.bg; g.fillRect(0, 0, PW, PH);
  const asp = S.aspect || 16 / 9;                 // formato del montaggio (16:9 o 9:16)
  let w = PW, h = PW / asp;
  if (h > PH) { h = PH; w = PH * asp; }
  const x0 = (PW - w) / 2, y0 = (PH - h) / 2, t = previewTime(), i = pieceAt(t);
  g.fillStyle = "#000"; g.fillRect(x0, y0, w, h);
  g.textAlign = "center"; g.textBaseline = "middle";
  if (i < 0) {
    g.fillStyle = "#777"; g.font = "13px " + font;
    g.fillText(marks.length ? "fuori dal montaggio" : "nessun marker", x0 + w / 2, y0 + h / 2);
    $("prevInfo").textContent = marks.length ? "Dopo l'ultimo marker: qui il montaggio è finito." : "Metti dei marker per vedere il montato.";
    return;
  }
  const p = plan[i], cl = S.clips[p.c], src = p.s0 + (t - p.t0);
  if ($("prevMode").value === "bn") {
    const white = i % 2 === 1;
    g.fillStyle = white ? "#fff" : "#000"; g.fillRect(x0, y0, w, h);
    g.fillStyle = white ? "#000" : "#fff"; g.font = "bold " + Math.round(Math.min(w, h) / 4) + "px " + font;
    g.fillText(String(i + 1), x0 + w / 2, y0 + h / 2);
  } else {
    const sp = loadSprite(p.c);
    if (sp.ok) {
      const idx = Math.max(0, Math.min(cl.n - 1, Math.round(src * cl.f)));
      const tw = sp.img.naturalWidth / cl.cols, th = sp.img.naturalHeight / cl.rows;
      const sc = Math.min(w / tw, h / th), dw = tw * sc, dh = th * sc;   // come "adatta" di Resolve
      g.drawImage(sp.img, (idx % cl.cols) * tw, Math.floor(idx / cl.cols) * th, tw, th,
                  x0 + (w - dw) / 2, y0 + (h - dh) / 2, dw, dh);
    } else {
      g.fillStyle = "#999"; g.font = "13px " + font;
      g.fillText(sp.err ? "anteprima non disponibile" : "preparo le miniature…", x0 + w / 2, y0 + h / 2);
    }
  }
  if (play && i !== lastPiece) flashUntil = performance.now() + 140;   // lampo sul cambio clip
  lastPiece = i;
  if (performance.now() < flashUntil) {
    g.strokeStyle = col.head; g.lineWidth = 5; g.strokeRect(x0 + 2.5, y0 + 2.5, w - 5, h - 5);
    dirty = true;
  }
  $("prevInfo").innerHTML = "Stacco <b>" + (i + 1) + "</b> di " + plan.length + " · " +
    (cl ? cl.name.replace(/</g, "&lt;") : "") + " da " + fmt(src) + (p.rep ? " · <b>ripetuto</b>" : "");
}
function regen(why) {
  const dur = Math.max(1, parseFloat($("inDur").value) || S.dur);
  const pts = fromBeats($("selStyle").value, dur);
  commit(() => { marks = pts.slice(); });
  sel = -1; updStatus();
  note = "marker rifatti" + (why ? " (" + why + ")" : "") + " · Ctrl+Z per tornare indietro";
  noteUntil = performance.now() + 5000;
  $("saved").textContent = note;
}
let note = "", noteUntil = 0;
function withNote(s) { return performance.now() < noteUntil ? note + " · " + s : s; }

// ---------------------------------------------------------------- vista
function setHead(t) {
  head = Math.min(Math.max(t, 0), DUR);
  if (play) startPlay(head);
  ensureVisible(head); updClock(); dirty = true;
}
function ensureVisible(t) {
  const vs = W * spp;
  if (t < t0 || t > t0 + vs) { t0 = t - vs * 0.35; clampView(); }
}
function zoomBy(f, xc) {
  if (xc === undefined) { const c = curTime(); xc = (c >= t0 && c <= t0 + W * spp) ? X(c) : W / 2; }
  const tm = T(xc);
  spp *= f; clampView(); t0 = tm - xc * spp; clampView();
  dirty = true; updStatus();
}
function fitAll() { spp = DUR / Math.max(W, 1); t0 = 0; clampView(); dirty = true; updStatus(); }
function resize() {
  DPR = window.devicePixelRatio || 1;
  const r = main.getBoundingClientRect(), o = ov.getBoundingClientRect();
  W = Math.max(50, Math.floor(r.width)); H = Math.max(80, Math.floor(r.height));
  OW = Math.max(50, Math.floor(o.width)); OH = Math.max(20, Math.floor(o.height));
  main.width = W * DPR; main.height = H * DPR; ov.width = OW * DPR; ov.height = OH * DPR;
  const p = pv.getBoundingClientRect();
  PW = Math.floor(p.width); PH = Math.floor(p.height);
  pv.width = Math.max(1, PW * DPR); pv.height = Math.max(1, PH * DPR);
  if (buf) { clampView(); renderOvImg(); updStatus(); }
  dirty = true;
}
function markAt(x) {
  let best = -1, bd = 7;
  for (let i = 0; i < marks.length; i++) { const d = Math.abs(X(marks[i]) - x); if (d <= bd) { bd = d; best = i; } }
  return best;
}

// ---------------------------------------------------------------- stato e salvataggio
function updButtons() {
  $("bPlay").textContent = play ? "❚❚ Pausa" : "▶ Play";
  $("bSnap").classList.toggle("on", snap);
  $("bScrub").classList.toggle("on", scrub);
}
function updStatus() {
  const n = marks.length;
  let short = Infinity, prev = 0;
  for (const m of marks) { short = Math.min(short, m - prev); prev = m; }
  $("stMarks").textContent = n;
  $("stShort").textContent = n ? short.toFixed(2) + " s (" + Math.round(short * S.fps) + " fot.)" : "—";
  $("stEnd").textContent = n ? fmt(marks[n - 1]) : "—";
  $("stSel").textContent = sel >= 0 && sel < n ? fmt(marks[sel]) : "—";
  const vs = W * spp;
  $("stZoom").textContent = vs < 10 ? vs.toFixed(1) + " s" : Math.round(vs) + " s";
}
function updClock() { $("stHead").textContent = fmt(curTime()); }
let saveTimer = null;
function scheduleSave() { $("saved").textContent = withNote("salvo…"); clearTimeout(saveTimer); saveTimer = setTimeout(save, 250); }
async function save() {
  try {
    const r = await fetch("/markers", {method: "POST", headers: {"Content-Type": "application/json"},
                                       body: JSON.stringify({session: S.session, marks: marks})});
    $("saved").textContent = withNote(r.ok ? "✓ salvato" : "⚠ non salvato");
  } catch (e) { $("saved").textContent = "⚠ BeaTonic non raggiungibile"; }
}

// ---------------------------------------------------------------- mouse
main.addEventListener("pointerdown", e => {
  ensureAudio();
  const x = e.offsetX, i = markAt(x);
  if (e.button === 2) { if (i >= 0) delMark(i); return; }
  if (e.button !== 0) return;
  main.setPointerCapture(e.pointerId);
  if (i >= 0) { sel = i; drag = {kind: "mark", i: i, x0: x, moved: false, undo: false}; updStatus(); }
  else { drag = {kind: "pan", x0: x, t00: t0, moved: false}; main.style.cursor = "grabbing"; }
  dirty = true;
});
main.addEventListener("pointermove", e => {
  const x = e.offsetX;
  if (drag) {
    if (Math.abs(x - drag.x0) > 3) drag.moved = true;
    if (drag.kind === "pan") {
      if (drag.moved) { t0 = drag.t00 - (x - drag.x0) * spp; follow = false; clampView(); }
    } else if (drag.moved) {
      if (!drag.undo) { pushUndo(); drag.undo = true; }
      let t = Math.min(Math.max(T(x), 0.001), DUR);
      if (!e.altKey) t = snapT(t, 8);
      marks[drag.i] = t;
      scrubAt(t);
      updStatus();
    }
  } else {
    hoverT = T(x);
    hoverMark = markAt(x);
    main.style.cursor = hoverMark >= 0 ? "ew-resize" : "grab";
    if (!play) scrubAt(hoverT);
  }
  dirty = true;
});
main.addEventListener("pointerup", e => {
  if (drag) {
    const x = e.offsetX;
    if (drag.kind === "pan" && !drag.moved) { sel = -1; setHead(T(x)); }
    if (drag.kind === "mark") {
      if (drag.moved) {
        const t = marks[drag.i];
        marks.sort((a, b) => a - b); sel = marks.indexOf(t); dedupe(); changed();
      } else setHead(marks[drag.i]);
    }
  }
  drag = null; main.style.cursor = "grab"; updStatus(); dirty = true;
});
main.addEventListener("dblclick", e => { addMark(T(e.offsetX), true); });
main.addEventListener("contextmenu", e => e.preventDefault());
main.addEventListener("pointerleave", () => { hoverT = null; hoverMark = -1; dirty = true; });
main.addEventListener("wheel", e => {
  e.preventDefault();
  if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    t0 += (e.deltaX || e.deltaY) * spp; follow = false; clampView();
  } else zoomBy(Math.exp(e.deltaY * 0.0015), e.offsetX);
  dirty = true;
}, {passive: false});
function ovJump(x) { t0 = x / OW * DUR - W * spp / 2; follow = false; clampView(); dirty = true; }
ov.addEventListener("pointerdown", e => {
  ensureAudio(); ov.setPointerCapture(e.pointerId);
  ovDrag = {x0: e.offsetX, moved: false}; ovJump(e.offsetX);
});
ov.addEventListener("pointermove", e => {
  if (!ovDrag) return;
  if (Math.abs(e.offsetX - ovDrag.x0) > 3) ovDrag.moved = true;
  ovJump(e.offsetX);
});
ov.addEventListener("pointerup", e => {
  if (ovDrag && !ovDrag.moved) setHead(e.offsetX / OW * DUR);
  ovDrag = null;
});
ov.addEventListener("wheel", e => { e.preventDefault(); zoomBy(Math.exp(e.deltaY * 0.0015)); }, {passive: false});

// ---------------------------------------------------------------- tastiera e pulsanti
window.addEventListener("keydown", e => {
  if (!buf || e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  ensureAudio();
  const k = e.key, ctrl = e.ctrlKey || e.metaKey;
  if (k === " ") { e.preventDefault(); togglePlay(); }
  else if (ctrl && (k === "z" || k === "Z")) { e.preventDefault(); if (e.shiftKey) doRedo(); else doUndo(); }
  else if (ctrl && (k === "y" || k === "Y")) { e.preventDefault(); doRedo(); }
  else if (ctrl) return;
  else if (k === "m" || k === "M" || k === "q" || k === "Q") markHere();
  else if (k === "p" || k === "P") togglePrev();
  else if (k === "Delete" || k === "Backspace") { e.preventDefault(); delMark(sel); }
  else if (k === "ArrowLeft" || k === "ArrowRight") {
    e.preventDefault();
    const d = (k === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1) / (S.fps || 25);
    if (sel >= 0) nudgeSel(d); else setHead(curTime() + d);
  }
  else if (k === "ArrowUp") { e.preventDefault(); jumpMark(-1); }
  else if (k === "ArrowDown") { e.preventDefault(); jumpMark(1); }
  else if (k === "+" || k === "=") zoomBy(1 / 1.6);
  else if (k === "-" || k === "_") zoomBy(1.6);
  else if (k === "0") fitAll();
  else if (k === "s" || k === "S") { scrub = !scrub; updButtons(); }
  else if (k === "n" || k === "N") { snap = !snap; updButtons(); }
  else if (k === "f" || k === "F") toggleFull();
  else if (k === "h" || k === "H" || k === "?") $("help").hidden = !$("help").hidden;
  else if (k === "Home") setHead(0);
  else if (k === "End") setHead(marks.length ? marks[marks.length - 1] : DUR);
  else if (k === "Escape") { sel = -1; $("help").hidden = true; updStatus(); }
  else return;
  dirty = true;
});
function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}
function on(id, fn) { $(id).addEventListener("click", ev => { ensureAudio(); fn(ev); ev.currentTarget.blur(); dirty = true; }); }
on("bPlay", togglePlay);
on("bAdd", () => addMark(curTime(), true));
on("bDel", () => delMark(sel));
on("bSnap", () => { snap = !snap; updButtons(); });
on("bScrub", () => { scrub = !scrub; updButtons(); });
on("bUndo", doUndo);
on("bRedo", doRedo);
on("bRegen", () => regen());
on("bPrev", () => togglePrev());
function togglePrev() {
  prevOn = !prevOn;
  $("prev").hidden = !prevOn;
  $("bPrev").classList.toggle("on", prevOn);
  resize();
}
$("selStyle").addEventListener("change", e => {          // cambia stile -> marker subito
  regen("stile " + e.target.options[e.target.selectedIndex].text); e.target.blur(); dirty = true;
});
$("inDur").addEventListener("change", e => { regen("durata " + e.target.value + " s"); e.target.blur(); dirty = true; });
$("prevMode").addEventListener("change", e => { e.target.blur(); dirty = true; });
on("bClear", () => { commit(() => { marks = []; }); sel = -1; updStatus(); });
on("bFit", fitAll);
on("bFull", toggleFull);
on("bHelp", () => { $("help").hidden = false; });
on("bHelpClose", () => { $("help").hidden = true; });
on("bDone", async () => {
  stopPlay(); clearTimeout(saveTimer); await save();
  try { await fetch("/done", {method: "POST", body: JSON.stringify({session: S.session, n: marks.length})}); } catch (e) {}
  $("doneN").textContent = marks.length; $("doneBox").hidden = false;
});
on("bKeep", () => { $("doneBox").hidden = true; });
on("bClose", () => { window.close(); });
$("help").addEventListener("click", e => { if (e.target.id === "help") $("help").hidden = true; });
window.addEventListener("resize", resize);
document.addEventListener("fullscreenchange", () => setTimeout(resize, 50));

// ---------------------------------------------------------------- ciclo di disegno
let lastClock = 0;
function frame(now) {
  if (play) {
    const c = curTime();
    if (follow && W * spp < DUR) { t0 = c - W * spp * 0.35; clampView(); }
    dirty = true;
    if (now - lastClock > 90) { updClock(); lastClock = now; }
  }
  if (dirty && buf) { drawMain(); drawOv(); drawPreview(); dirty = false; }
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- avvio
async function boot() {
  try { S = await (await fetch("/session", {cache: "no-store"})).json(); }
  catch (e) { $("startMsg").textContent = "Non trovo la sessione: riapri l'editor da BeaTonic."; return; }
  document.body.classList.toggle("labs", S.theme === "labs");
  readColors();
  $("song").textContent = S.song_name || "";
  if (S.logo) { $("logo").src = "/logo"; $("logo").hidden = false; }
  for (const st of S.styles) {
    const o = document.createElement("option");
    o.value = st[1]; o.textContent = st[0]; o.selected = st[0] === S.style;
    $("selStyle").appendChild(o);
  }
  $("inDur").value = Math.round(S.dur);
  marks = (S.marks || []).slice().sort((a, b) => a - b);
  $("startMsg").textContent = "Carico «" + (S.song_name || "la canzone") + "»…";
  const Ctx = window.AudioContext || window.webkitAudioContext;
  ctx = new Ctx();
  let data;
  try { data = await (await fetch("/song", {cache: "no-store"})).arrayBuffer(); }
  catch (e) { $("startMsg").textContent = "Non riesco a leggere la canzone."; return; }
  $("startMsg").textContent = "Leggo l'audio…";
  try { buf = await ctx.decodeAudioData(data); }
  catch (e) { $("startMsg").textContent = "Il browser non riesce a leggere questo formato audio."; return; }
  SR = buf.sampleRate; DUR = buf.duration;
  buildPeaks();
  resize();
  spp = Math.min(12, DUR) / Math.max(W, 1); t0 = 0; clampView();
  if (!(S.tape && S.tape.length)) { prevOn = false; $("prev").hidden = true; $("bPrev").hidden = true; resize(); }
  buildPlan(); renderOvImg(); updButtons(); updStatus(); updClock();
  const go = () => { ensureAudio(); $("start").hidden = true; dirty = true; };
  if (ctx.state === "running") go();
  else {
    $("startMsg").innerHTML = "Pronto. <b>Clicca qui per iniziare</b> (serve un clic per attivare l'audio).";
    $("start").style.cursor = "pointer";
    $("start").addEventListener("click", go, {once: true});
  }
  requestAnimationFrame(frame);
}
boot();
setInterval(() => fetch("/api/ping").catch(() => {}), 4000);   // "sono aperto": BeaTonic resta vivo
(function favicon() {                         // logo Plutonic quadrato: icona della finestra e della barra
  const im = new Image();
  im.onload = () => {
    const c = document.createElement("canvas"), g = c.getContext("2d");
    c.width = c.height = 128;
    const s = Math.min(128 / im.width, 128 / im.height);
    g.drawImage(im, (128 - im.width * s) / 2, (128 - im.height * s) / 2, im.width * s, im.height * s);
    $("fav").href = c.toDataURL("image/png");
  };
  im.src = "/logo";
})();
</script>
</body>
</html>
"""


SESS = {}                 # sessione dell'editor (beat, marker, clip...) servita a /session
STATE = {"log": "", "status": "Pronto.", "busy": "", "quit": False, "pinged": 0.0,
         "sel": {"videos": [], "song": ""}}
CMDQ = queue.Queue()      # comandi dalla finestra, eseguiti dal plugin uno alla volta
SERVER = {"srv": None, "port": None}
MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".ogg": "audio/ogg", ".flac": "audio/flac"}
_sprite_locks = {}


def _sprite(i):
    """Miniature di una clip in UN'immagine a mosaico (anteprima del montato),
    create con ffmpeg la prima volta che servono."""
    c = SESS["clips"][i]
    out = os.path.join(WEB, "thumb_%s_%d.jpg" % (SESS["session"], i))
    with _sprite_locks.setdefault(i, threading.Lock()):
        if not os.path.exists(out):
            vf = ("fps=%g,scale='if(gt(iw,ih),200,-2)':'if(gt(iw,ih),-2,200)',tile=%dx%d"
                  % (c["f"], c["cols"], c["rows"]))
            subprocess.run([FFMPEG, "-y", "-v", "error", "-i", c["path"], "-an", "-vf", vf,
                            "-frames:v", "1", "-q:v", "5", out + ".tmp.jpg"],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=600,
                           creationflags=_NOWIN)
            if os.path.exists(out + ".tmp.jpg"):
                os.replace(out + ".tmp.jpg", out)
    return out


class _Handler(BaseHTTPRequestHandler):
    """Server SOLO su 127.0.0.1: pannello (/), editor (/editor), dati e comandi (/api)."""

    def log_message(self, *a):
        pass

    def _send(self, data, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, p, ctype):
        try:
            n = os.path.getsize(p)
        except (OSError, TypeError):
            return self._send(b"not found", "text/plain", 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(n))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_GET(self):
        STATE["pinged"] = time.time()
        p = self.path.split("?")[0]
        try:
            if p == "/":
                self._send(MAIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif p == "/editor":
                self._send(EDITOR_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif p == "/api/state":
                self._send(json.dumps(_state()).encode("utf-8"))
            elif p == "/api/ping":
                self._send(b"ok", "text/plain")
            elif p == "/session":
                self._send(json.dumps(SESS).encode("utf-8"))
            elif p == "/song":
                s = SESS.get("song_path", "")
                self._file(s, MIME.get(os.path.splitext(s)[1].lower(), "application/octet-stream"))
            elif p in ("/logo", "/favicon.ico"):
                self._file(_LOGO, "image/png")
            elif p.startswith("/thumb/"):
                self._file(_sprite(int(p.split("/")[2])), "image/jpeg")
            else:
                self._send(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send(str(e).encode("utf-8"), "text/plain", 500)
            except Exception:
                pass

    def do_POST(self):
        STATE["pinged"] = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        p = self.path.split("?")[0]
        if p in ("/markers", "/done"):                # salvataggi dell'editor
            name = os.path.join(WEB, "markers.json" if p == "/markers" else "done.json")
            with open(name + ".tmp", "wb") as f:
                f.write(body)
            os.replace(name + ".tmp", name)
            return self._send(b"ok", "text/plain")
        try:
            d = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            d = {}
        if p == "/api/set":
            _apply_settings(d)
        elif p == "/api/cmd":
            _apply_settings(d.get("set") or {})
            CMDQ.put((d.get("cmd"), d.get("arg")))
        else:
            return self._send(b"not found", "text/plain", 404)
        self._send(b"ok", "text/plain")


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    SERVER.update(srv=srv, port=srv.server_address[1])
    return SERVER["port"]


def _url(path=""):
    return "http://127.0.0.1:%d/%s" % (SERVER["port"], path)


def _open_window(url, size=(1500, 880)):
    """Finestra pulita (senza schede ne' barra) con Chrome o Edge; se mancano, il browser
    predefinito. Icona della finestra e della barra = logo Plutonic (favicon)."""
    flags = ["--app=" + url, "--new-window", "--no-first-run", "--no-default-browser-check",
             "--autoplay-policy=no-user-gesture-required", "--window-size=%d,%d" % size,
             "--user-data-dir=" + os.path.join(WEB, "profilo_browser")]
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                          os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands += [shutil.which(n) for n in ("google-chrome", "chromium", "microsoft-edge")]
    for c in cands:
        if c and os.path.exists(c):
            try:
                subprocess.Popen([c] + flags, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=_NOWIN)
                return True
            except Exception:
                pass
    import webbrowser
    return webbrowser.open(url)


def _preview_data(tape):
    """Clip e sequenza delle shot (nell'ordine del montaggio) per l'anteprima nell'editor.
    Per ogni clip: dati del mosaico di miniature (al massimo ~300 per clip)."""
    clips, index, seq = [], {}, []
    for sh in tape:
        c = sh["clip"]
        key = c.GetClipProperty("File Path") or c.GetName()
        if key not in index:
            dur = max(0.1, _clip_dur_sec(c))
            f = min(4.0, 300.0 / dur)
            n = int(dur * f) + 1
            index[key] = len(clips)
            clips.append({"name": c.GetName(), "path": key, "f": round(f, 4), "n": n,
                          "cols": 10, "rows": int(math.ceil(n / 10.0))})
        seq.append({"c": index[key], "in": sh["in"], "out": sh["out"]})
    return clips, seq


def editor_open(song, marks, beats, bpos, intens, bpm, fps, style, dur, theme,
                tape=(), reuse=True, aspect=16 / 9.0):
    """Prepara la sessione e apre l'editor. Ritorna (ok, messaggio)."""
    os.makedirs(WEB, exist_ok=True)
    src = song.GetClipProperty("File Path") or ""
    path = src
    if os.path.splitext(src)[1].lower() not in BROWSER_AUDIO:   # es. .aiff/.wma
        path = os.path.join(WEB, "canzone.wav")
        subprocess.run([FFMPEG, "-y", "-i", src, "-ar", "44100", path],
                       capture_output=True, creationflags=_NOWIN)
    sess = {"session": SESSION[0], "song_name": song.GetName(), "song_path": path,
            "beats": beats, "bpos": bpos, "intens": intens, "bpm": bpm, "marks": marks,
            "fps": fps, "theme": theme, "style": style, "dur": dur, "min_cut": MIN_CUT,
            "styles": [[k, v] for k, v in STYLES.items()] +
                      [["Proposta " + name, key] for key, name, _d, _o in PRESETS],
            "logo": _LOGO if os.path.exists(_LOGO) else ""}
    sess["clips"], sess["tape"] = _preview_data(tape)
    sess.update(reuse=bool(reuse), aspect=aspect, ffmpeg=FFMPEG)
    tmp = os.path.join(WEB, "session.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sess, f)
    os.replace(tmp, os.path.join(WEB, "session.json"))
    SESS.clear()
    SESS.update(sess)
    url = _url("editor?s=%s" % SESSION[0])
    _open_window(url, (1500, 880))
    return True, url


def editor_marks():
    """Marker salvati dall'editor per QUESTA analisi, o None."""
    try:
        with open(os.path.join(WEB, "markers.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if d.get("session") != SESSION[0]:
        return None
    return sorted(float(t) for t in d.get("marks", []) if float(t) > 0)


def montage(shots, song, ivs, tag, fmt, order_mode, reuse, set_log, seed=None):
    """Monta le shot negli intervalli ivs [(inizio s, fine s), ...] che partono da 0.
    Ogni stacco cade ESATTAMENTE sul suo fotogramma: dopo ogni clip si legge dove e'
    finita davvero e la successiva si calcola per arrivare al punto dopo (nessuno
    slittamento che si accumula, anche con clip a fps diversi dalla timeline)."""
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()
    if not shots:
        return "Nessuna shot: analizza prima le clip."
    if not ivs:
        return "Nessun punto di stacco."
    tape = order_shots(shots, order_mode, seed)

    new = _new_timeline(mp, "BeaTonic_%s" % tag)
    if new is None:
        return "Impossibile creare la timeline."
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

    # Ogni stacco = UNA shot che lo copre tutto (niente code di pochi fotogrammi).
    # Si parte dalla shot successiva nell'ordine scelto, quindi due stacchi di fila
    # non mostrano la stessa clip; ogni ritorno a una clip ne mostra un pezzo nuovo.
    # Quando nessuna shot ha piu' abbastanza materiale nuovo: nuovo giro (riusa)
    # oppure stop. Uno stacco si spezza SOLO se nessuna shot e' lunga quanto lui
    # (sarebbe un taglio in piu' rispetto ai marker), e mai sotto MIN_CUT.
    shot_i = [0]
    read_pos = {}          # id(shot) -> secondi gia' usati di quella shot (in questo giro)
    exhausted = [False]
    giri = [0]             # quante volte il materiale nuovo e' finito e si e' ricominciato

    def remaining(sh):
        return sh["out"] - sh["in"] - read_pos.get(id(sh), 0.0)

    def take(sh, amount):
        start = sh["in"] + read_pos.get(id(sh), 0.0)
        read_pos[id(sh)] = start + amount - sh["in"]
        return (sh["clip"], sh["fps"], start, start + amount)

    def find_whole(need):
        for k in range(len(tape)):
            idx = (shot_i[0] + k) % len(tape)
            if remaining(tape[idx]) >= need:
                return idx
        return None

    def pull(length):
        """Lista di (clip, fps, src_in, src_out) lunga length s."""
        idx = find_whole(length)
        if idx is None and reuse:
            read_pos.clear()                    # materiale nuovo finito: nuovo giro
            giri[0] += 1
            idx = find_whole(length)
        if idx is not None:
            shot_i[0] = idx + 1
            return [take(tape[idx], length)]
        # Nessuna shot copre lo stacco da sola (es. un'intro lunga senza beat): lo si
        # divide in parti UGUALI, mai piu' corte di MIN_CUT; se non si puo', si chiude.
        longest = max(remaining(s) for s in tape)
        n = int(math.ceil(length / longest - 1e-9)) if longest > 0.02 else 0
        if not n or length / n < MIN_CUT:
            exhausted[0] = True
            return []
        part = length / n
        pieces = []
        for _ in range(n):
            idx = find_whole(part)
            if idx is None:                     # stacco monco: si chiude su quello prima
                exhausted[0] = True
                return []
            shot_i[0] = idx + 1
            pieces.append(take(tape[idx], part))
        return pieces

    ok, rep = 0, 0
    cur = ns                                    # dove finisce DAVVERO il montaggio
    for _, be in ivs:
        target = ns + int(round(be * tl_fps))   # fotogramma esatto del prossimo stacco
        if target - cur <= 0:
            continue
        pieces = pull((target - cur) / tl_fps)
        if not pieces:
            break
        cur0, acc = cur, 0.0
        for k, (clip, cfps, si, so) in enumerate(pieces):
            acc += so - si
            end = target if k == len(pieces) - 1 else cur0 + int(round(acc * tl_fps))
            plen = end - cur                    # fotogrammi di timeline da riempire
            if plen <= 0:
                continue
            sf = int(round(si * cfps))
            ef = sf + max(1, int(round(plen * cfps / tl_fps)))
            info = {"mediaPoolItem": clip, "startFrame": sf, "endFrame": ef,
                    "trackIndex": 1, "mediaType": 1}
            try:
                items = mp.AppendToTimeline([info])   # SEQUENZIALE = senza buchi
            except Exception:
                items = None
            if items:
                ok += 1
                rep += 1 if giri[0] else 0      # video gia' mostrato in un giro prima
                try:
                    cur = items[0].GetStart() + items[0].GetDuration()
                except Exception:
                    cur += plen
        if exhausted[0]:
            break
    frames = cur - ns
    _put_song(mp, song, ns, frames)             # canzone su A1, lunga quanto il montaggio
    # Riepilogo: si capisce subito se il materiale basta o se si e' dovuto ripetere
    total = sum(s["out"] - s["in"] for s in shots)
    nclip = len(set(s["clip"].GetName() for s in shots))
    mm = lambda t: "%d:%02d" % (int(t // 60), int(t % 60))
    want = ivs[-1][1]
    lines = ["Montaggio '%s' creato: %d stacchi su %.1fs, %s." % (new.GetName(), ok, frames / tl_fps, fmt),
             "• Materiale: %d pezzi da %d clip, %s di video in tutto." % (len(shots), nclip, mm(total))]
    if rep:
        lines.append("• RIPETIZIONI: %d stacchi su %d riusano video già visto (il materiale è "
                     "ricominciato da capo %d volt%s): non basta a coprire %s di montaggio senza "
                     "ripetere. Aggiungi clip o accorcia la durata." % (
                         rep, ok, giri[0], "a" if giri[0] == 1 else "e", mm(want)))
    elif exhausted[0] and frames / tl_fps < want - 0.05:
        lines.append("• Il materiale è finito a %.1fs su %.1fs (riusa clip spento): "
                     "montaggio più corto, nessuna ripetizione." % (frames / tl_fps, want))
    else:
        lines.append("• Nessuna ripetizione: ogni pezzo di video compare una volta sola.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Adatta la canzone alla durata (come l'AI Music Editor di Resolve, che i plugin
# non possono usare): toglie o ripete battute intere dove la musica "suona uguale"
# (stesso accordo, stesso timbro, stessa posizione nella frase), tenendo l'inizio
# e il finale veri; dissolvenza brevissima centrata sul "1" della battuta.
# --------------------------------------------------------------------------
FIT_XFADE = 0.06          # s: dissolvenza sul punto di salto
FIT_TEMPO = 0.025         # correzione di tempo massima per la durata esatta (2,5%)


def _audio_info(path, known=0.0):
    """(durata s, fine della musica s: senza il silenzio in coda).
    L'output di ffmpeg si legge in BYTE: con text=True la codifica di Windows (cp1252)
    si rompe sui tag delle canzoni e il testo arriva vuoto senza nessun errore."""
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-vn", "-af",
                            "silencedetect=n=-50dB:d=0.7", "-f", "null", "-"],
                           capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=600, creationflags=_NOWIN)
        txt = (p.stderr or b"").decode("utf-8", "replace")
    except Exception:
        txt = ""
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    if dur <= 0:                         # durata illeggibile: quella gia' nota dall'analisi
        return known, known
    st = re.findall(r"silence_start: ([\d.]+)", txt)
    en = re.findall(r"silence_end: ([\d.]+)", txt)
    end = dur
    if st and len(st) > len(en):         # silenzio fino alla fine del file
        end = min(dur, float(st[-1]) + 0.4)
    return dur, end


def _beat_feats(path, beats):
    """Per ogni beat: (accordo 12 note, timbro 12 bande), vettori normalizzati.
    Con numpy: FFT vera (cromagramma = l'armonia). Senza: solo energia nel tempo."""
    try:
        import numpy as np
    except ImportError:
        np = None
    sr = 22050
    p = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True, creationflags=_NOWIN)
    raw = p.stdout
    edges = [int(b * sr) for b in beats]
    if np is not None:
        y = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        n_fft, hop = 4096, 1024
        win = np.hanning(n_fft).astype(np.float32)
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
        ok = np.where((freqs >= 55) & (freqs <= 2000))[0]
        pc = np.zeros((len(freqs), 12), np.float32)
        pc[ok, np.round(12 * np.log2(freqs[ok] / 440.0) + 69).astype(int) % 12] = 1.0
        bands = np.geomspace(60, 8000, 13)
        bm = np.zeros((len(freqs), 12), np.float32)
        for k in range(12):
            bm[(freqs >= bands[k]) & (freqs < bands[k + 1]), k] = 1.0
        nfr = max(0, 1 + (len(y) - n_fft) // hop)
        chroma = np.zeros((nfr, 12), np.float32)
        timbre = np.zeros((nfr, 12), np.float32)
        for s in range(0, nfr, 512):                    # a blocchi: poca memoria
            e = min(nfr, s + 512)
            idx = np.arange(s, e)[:, None] * hop + np.arange(n_fft)[None, :]
            S = np.abs(np.fft.rfft(y[idx] * win, axis=1))
            chroma[s:e] = np.log1p(S @ pc)
            timbre[s:e] = np.log1p(S @ bm)
        out = []
        for i in range(len(beats) - 1):
            a = min(nfr - 1, edges[i] // hop)
            z = min(nfr, max(a + 1, edges[i + 1] // hop))
            c, t = chroma[a:z].mean(0), timbre[a:z].mean(0)
            c = c - c.mean()                            # accordo: quali note spiccano
            c = c / (np.linalg.norm(c) or 1.0)
            t = t - t.mean()
            t = t / (np.linalg.norm(t) or 1.0)
            out.append((c.tolist(), t.tolist()))
        return out
    data = struct.unpack("<%dh" % (len(raw) // 2), raw)   # ripiego senza numpy
    out = []
    for i in range(len(beats) - 1):
        seg = data[edges[i]:edges[i + 1]:4]
        q = max(1, len(seg) // 12)
        t = [math.log1p(sum(abs(v) for v in seg[k * q:(k + 1) * q]) / q) for k in range(12)]
        m = sum(t) / 12.0
        t = [v - m for v in t]
        nn = math.sqrt(sum(v * v for v in t)) or 1.0
        out.append(([], [v / nn for v in t]))
    return out


def _bar_dist(feats, bars):
    """Distanza tra battute (0 = identiche, 1 = niente in comune), beat per beat:
    accordo 60% + timbro 40% (solo timbro se manca numpy)."""
    wc, wt = (0.6, 0.4) if feats and feats[0][0] else (0.0, 1.0)
    rc, rt = math.sqrt(wc), math.sqrt(wt)
    V = []
    for k in range(len(bars)):
        v = []
        for c, t in feats[bars[k]:bars[k] + 4]:
            v += [x * rc for x in c] + [x * rt for x in t]
        V.append(v)
    try:
        import numpy as np
        A = np.array(V, dtype=np.float64)
        return np.clip(1.0 - A @ A.T / 4.0, 0.0, 2.0).tolist()
    except ImportError:
        n = len(V)
        D = [[0.0] * n for _ in range(n)]
        for p in range(n):
            for q in range(p, n):
                D[p][q] = D[q][p] = max(0.0, 1.0 - sum(a * b for a, b in zip(V[p], V[q])) / 4.0)
        return D


def fit_plan(beats, bpos, feats, song_end, target):
    """Sceglie dove saltare. Ritorna {segs: [(da, a)], length, jumps: [(da, a, somiglianza)],
    exact} oppure None. Prova 1 e 2 salti e tiene quelli che si sentono meno."""
    bars = [i for i, p in enumerate(bpos) if p == 0 and i + 4 <= len(feats)
            and beats[i] < song_end - 0.5]
    nb = len(bars)
    if nb < 12 or target <= 0 or song_end <= 0:
        return None
    T = [beats[i] for i in bars]
    D = _bar_dist(feats, bars)
    barlen = (T[-1] - T[0]) / (nb - 1)

    def cost(a, b):                      # suono fino alla battuta a, riprendo dalla battuta b
        c = [D[a][b]]
        if a >= 1 and b >= 1:
            c.append(D[a - 1][b - 1])
        if a + 1 < nb and b + 1 < nb:
            c.append(D[a + 1][b + 1])
        return sum(c) / len(c) + (0.0 if (b - a) % 4 == 0 else 0.08)

    def pack(segs, L, js):
        return {"segs": segs, "length": L, "exact": abs(L - target) <= FIT_TEMPO * target,
                "beat": barlen / 4.0,
                "jumps": [(T[a], T[b], max(0.0, 1.0 - cost(a, b))) for a, b in js]}

    if abs(song_end - target) <= FIT_TEMPO * target:      # gia' della durata giusta
        return pack([(0.0, song_end)], song_end, [])
    best = None
    for tol in (FIT_TEMPO * target, 0.55 * barlen):        # prima esatta, poi la piu' vicina
        if target < song_end:                              # ---------- ACCORCIARE
            for a in range(2, nb - 1):                     # 1 salto: inizio + finale
                for b in range(a + 2, nb):
                    L = T[a] + song_end - T[b]
                    err = abs(L - target)
                    if err <= tol:
                        sc = cost(a, b) + 0.3 * err / barlen
                        if best is None or sc < best[0]:
                            best = (sc, [(a, b)], L)
            cand = {}                                      # 2 salti: le destinazioni migliori
            for a in range(2, nb - 1):
                cs = sorted((cost(a, b), b) for b in range(a + 2, nb))[:8]
                cand[a] = [(c, b) for c, b in cs if c < 0.5]
            for a1 in cand:
                for c1, b1 in cand[a1]:
                    for a2 in range(b1 + 2, nb - 1):
                        for c2, b2 in cand.get(a2, ()):
                            L = T[a1] + (T[a2] - T[b1]) + (song_end - T[b2])
                            err = abs(L - target)
                            if err <= tol:
                                sc = c1 + c2 + 0.04 + 0.3 * err / barlen
                                if best is None or sc < best[0]:
                                    best = (sc, [(a1, b1), (a2, b2)], L)
        else:                                              # ---------- ALLUNGARE (giri)
            for a in range(4, nb):
                for b in range(1, a - 3):
                    loop = T[a] - T[b]
                    m = max(1, int(round((target - song_end) / loop)))
                    L = song_end + m * loop
                    err = abs(L - target)
                    if err <= tol:
                        sc = cost(a, b) + 0.03 * m + 0.3 * err / barlen
                        if best is None or sc < best[0]:
                            best = (sc, [(a, b)] * m, L)
        if best:
            break
    if not best:
        return None
    js = best[1]
    if target < song_end:
        segs, pos = [], 0.0
        for a, b in js:
            segs.append((pos, T[a]))
            pos = T[b]
        segs.append((pos, song_end))
    else:
        a, b = js[0]
        segs = [(0.0, T[a])] + [(T[b], T[a])] * (len(js) - 1) + [(T[b], song_end)]
    return pack(segs, best[2], js)


def _xfade(sim, beat):
    """Dissolvenza del salto: quasi invisibile se i due punti sono uguali, altrimenti
    lunga 1-2 beat a tempo (le due parti si fondono in battuta, come un DJ)."""
    if sim >= 0.85:
        return FIT_XFADE
    return beat if sim >= 0.7 else 2 * beat


def render_fit(src, plan, target, out):
    """Crea il file adattato: segmenti di battute intere uniti con una dissolvenza
    centrata sul "1", poi una correzione di tempo (<= 2,5%) per la durata esatta."""
    segs, n = plan["segs"], len(plan["segs"])
    xf = [_xfade(s, plan["beat"]) for _, _, s in plan["jumps"]] or [FIT_XFADE]
    xf += [xf[-1]] * (n - 1 - len(xf))
    fc = []
    for i, (s, e) in enumerate(segs):
        s2 = max(0.0, s - (xf[i - 1] / 2.0 if i > 0 else 0.0))
        e2 = e + (xf[i] / 2.0 if i < n - 1 else 0.0)
        fc.append("[0:a]atrim=start=%.5f:end=%.5f,asetpts=PTS-STARTPTS[s%d]" % (s2, e2, i))
    last = "s0"
    for i in range(1, n):
        fc.append("[%s][s%d]acrossfade=d=%.4f:c1=qsin:c2=qsin[x%d]" % (last, i, xf[i - 1], i))
        last = "x%d" % i
    length, ratio = plan["length"], plan["length"] / target
    if 0.0005 < abs(ratio - 1.0) <= FIT_TEMPO:
        fc.append("[%s]atempo=%.6f[out]" % (last, ratio))
        last, length = "out", target
    for s, e in segs:                    # mai un pezzo che finisce prima di cominciare
        if e - s < 0.05:
            raise RuntimeError("piano non valido: pezzo %.2f-%.2f s" % (s, e))
    try:
        p = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", src, "-vn",
                            "-filter_complex", ";".join(fc), "-map", "[%s]" % last,
                            "-c:a", "pcm_s24le", out],
                           capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=max(120.0, 3.0 * length), creationflags=_NOWIN)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg non ha finito in tempo (fermato).")
    if p.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((p.stderr or b"ffmpeg").decode("utf-8", "replace")[-300:])
    return length


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
# Pannello: una finestra a parte (come l'editor). Il plugin fa da "motore": esegue i
# comandi che arrivano dalla finestra (Analizza, Adatta, Marker, Monta) con le API di
# Resolve, mentre la finestra resta libera e mostra l'avanzamento in diretta.
# --------------------------------------------------------------------------
# Tema condiviso con PlutoniCut (studio/labs) in %APPDATA%\Plutonic\theme.txt
_THEME_FILE = os.path.join(os.environ.get("APPDATA", ""), "Plutonic", "theme.txt")
_SET_FILE = os.path.join(os.environ.get("APPDATA", ""), "Plutonic", "beatonic.json")
LOOKS = ("studio", "labs", "notte")      # aspetto della finestra: notte = scuro per l'editor


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


SET = {"sens": "Media (consigliata)", "style": list(STYLES)[0], "fmt": list(FORMATS)[0],
       "ord": list(ORDERS)[0], "dur": 30.0, "reuse": True, "look": load_theme()}
_CHOICES = {"sens": SENS, "style": STYLES, "fmt": FORMATS, "ord": ORDERS}


def _apply_settings(d, save=True):
    """Impostazioni dalla finestra (validate) e ricordate per la prossima volta."""
    for k, opts in _CHOICES.items():
        if d.get(k) in opts:
            SET[k] = d[k]
    try:
        if "dur" in d and float(d["dur"]) > 0:
            SET["dur"] = float(d["dur"])
    except (TypeError, ValueError):
        pass
    if "reuse" in d:
        SET["reuse"] = bool(d["reuse"])
    if d.get("look") in LOOKS and d["look"] != SET["look"]:
        SET["look"] = d["look"]
        save_theme("labs" if SET["look"] == "labs" else "studio")
    if save:
        try:
            os.makedirs(os.path.dirname(_SET_FILE), exist_ok=True)
            with open(_SET_FILE, "w", encoding="utf-8") as f:
                json.dump(SET, f)
        except Exception:
            pass


def _load_settings():
    try:
        with open(_SET_FILE, "r", encoding="utf-8") as f:
            _apply_settings(json.load(f), save=False)
    except Exception:
        pass


VIDEOS, SONG, SHOTS, BEATS, BPOS, BPM, INTENS = [], None, [], [], [], 0.0, []
MARKS = []          # punti di stacco dell'editor (s): ogni marker = un cambio clip
SEED = [int(time.time() * 1000) & 0x7fffffff]   # ordine casuale fisso: anteprima = montaggio
SONG_LEN = [0.0]    # durata della canzone in uso (s)
FITTED = {}         # canzone adattata in uso: orig_item, orig (file), orig_name, item, path
SHOT_CACHE = {}     # (file clip, sensibilita') -> ([(in, out)], n stacchi, n cambi inquadratura)
BEAT_CACHE = {}     # file canzone -> (bpm, beats, bpos, intens, durata)


def _path(c):
    try:
        return c.GetClipProperty("File Path") or c.GetName()
    except Exception:
        return ""


def _song_beats(item):
    """Beat della canzone: dalla memoria se l'ho gia' analizzata."""
    p = _path(item)
    if p not in BEAT_CACHE:
        bpm, beats, bpos, intens = beat_grid(p)
        BEAT_CACHE[p] = (bpm, beats, bpos, intens, _wav_dur(os.path.join(_TMP, "_song.wav")))
    return BEAT_CACHE[p]


def _use_song(item, analyze=False):
    """Canzone in uso + la sua analisi (se c'e'). Se cambia canzone, i marker ripartono."""
    global SONG, BPM, BEATS, BPOS, INTENS
    same = SONG is not None and item is not None and _path(SONG) == _path(item)
    SONG = item
    if not same:
        MARKS[:] = []
        SESSION[0] = "%x" % int(time.time() * 1000)
    data = None
    if item is not None:
        data = _song_beats(item) if analyze else BEAT_CACHE.get(_path(item))
    if data:
        BPM, BEATS, BPOS, INTENS, SONG_LEN[0] = data
    elif not same:
        BPM, BEATS, BPOS, INTENS, SONG_LEN[0] = 0.0, [], [], [], 0.0
    STATE["sel"] = {"videos": STATE["sel"]["videos"], "song": item.GetName() if item else ""}


def _sync_shots():
    """Pezzi video delle clip selezionate presi dalla memoria; False se qualcuna e' da analizzare."""
    global SHOTS
    key = str(SENS.get(SET["sens"]))
    out = []
    for c in VIDEOS:
        hit = SHOT_CACHE.get((_path(c), key))
        if hit is None:
            SHOTS = []
            return False
        fps = _clip_fps(c)
        out += [{"clip": c, "fps": fps, "in": a, "out": b} for a, b in hit[0]]
    SHOTS = out
    return True


def set_status(m):
    STATE["status"] = m


def set_log(m):
    STATE["log"] = m


def _state():
    """Quello che la finestra mostra (solo dati gia' pronti: niente API di Resolve qui,
    perche' questa funzione gira nei thread del server)."""
    saved = editor_marks()
    return {"sel": STATE["sel"], "set": SET, "log": STATE["log"], "status": STATE["status"],
            "busy": STATE["busy"], "quit": STATE["quit"], "presets": _presets_info(),
            "opts": {"sens": list(SENS), "style": list(STYLES), "fmt": list(FORMATS), "ord": list(ORDERS)},
            "info": {"analyzed": bool(BEATS and SHOTS), "shots": len(SHOTS), "bpm": BPM,
                     "beats": len(BEATS), "bars": sum(1 for p in BPOS if p == 0),
                     "song_len": SONG_LEN[0], "fitted": bool(FITTED),
                     "orig": FITTED.get("orig_name", ""),
                     "marks": len(saved) if saved is not None else len(MARKS)}}


def on_refresh(ev=None):
    """Rilegge la selezione del Media Pool. Se ho adattato la canzone, resta in uso la
    versione adattata finche' non selezioni una canzone DIVERSA dall'originale."""
    global VIDEOS
    VIDEOS, sel = selection()
    song = sel
    if FITTED and (sel is None or _path(sel) in (FITTED["orig"], FITTED["path"])):
        song = FITTED["item"]
    elif FITTED:
        FITTED.clear()                   # hai scelto un'altra canzone
    STATE["sel"] = {"videos": [c.GetName() for c in VIDEOS], "song": ""}
    _use_song(song)
    _sync_shots()
    set_status("Selezione: %d clip video · canzone: %s%s" % (
        len(VIDEOS), song.GetName() if song else "nessuna", " (adattata)" if FITTED else ""))
    if not FFMPEG:
        set_log("ffmpeg NON trovato. Installalo o imposta la variabile FFMPEG col percorso.")


def on_analyze(ev):
    """Analizza solo quello che non ho gia' analizzato (clip e canzone restano in memoria)."""
    on_refresh()
    if not FFMPEG:
        set_log("ffmpeg non trovato: impossibile analizzare."); return
    if not VIDEOS:
        set_log("Nessuna clip video selezionata nel Media Pool."); return
    if not SONG:
        set_log("Nessuna canzone (clip audio) selezionata nel Media Pool."); return
    params = SENS.get(SET["sens"], SENS["Media (consigliata)"])
    split, key = params is not None, str(params)
    todo = [c for c in VIDEOS if (_path(c), key) not in SHOT_CACHE]
    for i, c in enumerate(todo, 1):
        set_log("Analisi scene %d/%d: %s…" % (i, len(todo), c.GetName()))
        sh, nc, nr = build_shots([c], params, lambda m: None)
        SHOT_CACHE[(_path(c), key)] = ([(s["in"], s["out"]) for s in sh], nc, nr)
    _sync_shots()
    n_cut = sum(SHOT_CACHE[(_path(c), key)][1] for c in VIDEOS)
    n_ref = sum(SHOT_CACHE[(_path(c), key)][2] for c in VIDEOS)
    if not BEATS:
        set_log("Scene analizzate: %d pezzi video.\nAnalizzo i beat della canzone…" % len(SHOTS))
        _use_song(SONG, analyze=True)
    bars = sum(1 for p in BPOS if p == 0)
    reused = len(VIDEOS) - len(todo)
    set_log("ANALISI FATTA.\n• %d pezzi video da %d clip (%s)%s.\n• Canzone%s: %.0f BPM, %d beat, "
            "%d battute (stacco minimo %.2fs).\n\n"
            "Ora scegli stile, formato, durata e ordine. Puoi adattare la canzone alla durata "
            "e aprire l'editor dei marker, oppure premere direttamente 'Monta'."
            % (len(SHOTS), len(VIDEOS),
               ("%d stacchi + %d cambi d'inquadratura" % (n_cut, n_ref)) if split else "intere",
               (" — %d già analizzate, riusate" % reused) if reused else "",
               " adattata" if FITTED else "", BPM, len(BEATS), bars, MIN_CUT))
    set_status("Analisi ok: %d pezzi video · %.0f BPM · %d beat." % (len(SHOTS), BPM, len(BEATS)))


def _dur():
    return float(SET["dur"]) if SET["dur"] > 0 else 30.0


def _proj_fps():
    try:
        f = float(resolve.GetProjectManager().GetCurrentProject().GetSetting("timelineFrameRate"))
    except Exception:
        f = 25.0
    return f or 25.0


def marks_from_beats():
    return [b for _, b in _intervals(BEATS, BPOS, SET["style"], _dur(), INTENS, SONG_LEN[0])]


def _tape():
    """Le shot nell'ordine in cui verranno montate (stesso seme del montaggio)."""
    return order_shots(SHOTS, ORDERS.get(SET["ord"], "nameaz"), SEED[0])


def on_mark(ev):
    """Apre l'editor dei marker con la canzone, la forma d'onda e l'anteprima del montato."""
    if not BEATS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    saved = editor_marks()
    if saved is not None:                # riapre con le modifiche gia' fatte
        MARKS[:] = saved
    elif not MARKS:
        MARKS[:] = marks_from_beats()
    W, H = FORMATS.get(SET["fmt"], (1920, 1080))
    style = ev if isinstance(ev, str) else SET["style"]   # una proposta apre col suo taglio
    ok, info = editor_open(SONG, list(MARKS), BEATS, BPOS, INTENS, BPM, _proj_fps(),
                           style, _dur(), "labs" if SET["look"] == "labs" else "studio",
                           tape=_tape(), reuse=SET["reuse"], aspect=W / float(H))
    if not ok:
        set_log(info); set_status(info); return
    set_log("EDITOR DEI MARKER aperto in una finestra a parte (%d marker).\n\n"
            "Trascina per scorrere, rotella per lo zoom, passa col mouse sull'onda per "
            "ascoltare, Q o M = marker dove c'è il mouse, Spazio = play. A destra "
            "l'anteprima del montato. Si salva da solo.\n\n"
            "Quando hai finito premi 'Monta' qui: una clip tra un marker e l'altro, "
            "l'ultimo marker è la fine.\n\n(Se la finestra non si è aperta: %s)" % (len(MARKS), info))
    set_status("Editor aperto: sistema i marker, poi 'Monta'.")


def on_fit(ev):
    """Adatta la canzone alla durata scelta (inizio e finale veri, salti dove non si
    sentono), la importa nel Media Pool e la usa da qui in poi."""
    if not BEATS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    target = _dur()
    orig = FITTED.get("orig_item") or SONG   # riadattando si riparte SEMPRE dall'originale
    src = _path(orig)
    old = orig.GetName()
    set_status("Adatto la canzone a %.1f s…" % target)
    set_log("Adatto «%s» a %.1f s: confronto armonia, timbro e battute…" % (old, target))
    o_bpm, o_beats, o_bpos, o_int, o_len = _song_beats(orig)
    dur, end = _audio_info(src, o_len)
    if end <= 1.0:
        set_log("Non riesco a leggere la durata di «%s»." % old); return
    feats = _beat_feats(src, o_beats)
    plan = fit_plan(o_beats, o_bpos, feats, end, target)
    if not plan:
        set_log("Non riesco ad adattare questa canzone (troppe poche battute riconosciute)."); return
    name = "%s - BeaTonic %ss.wav" % (os.path.splitext(os.path.basename(src))[0], "%g" % round(target, 2))
    out = os.path.join(os.path.dirname(src), name)
    try:
        length = render_fit(src, plan, target, out)
    except Exception:                    # cartella della canzone non scrivibile
        out = os.path.join(_TMP, name)
        try:
            length = render_fit(src, plan, target, out)
        except Exception as e:
            set_log("Errore nel creare la canzone adattata:\n%s" % e); return
    items = None
    try:
        items = resolve.GetProjectManager().GetCurrentProject().GetMediaPool().ImportMedia([out])
    except Exception:
        items = None
    if not items:
        set_log("Canzone adattata creata ma non importata nel Media Pool:\n%s" % out); return
    FITTED.clear()
    FITTED.update(orig_item=orig, orig=src, orig_name=old, item=items[0], path=out)
    BEAT_CACHE.pop(_path(items[0]), None)  # file appena riscritto: va rianalizzato
    _use_song(items[0], analyze=True)      # le clip analizzate restano, cambia solo la canzone
    mm = lambda t: "%d:%04.1f" % (int(t // 60), t % 60)
    how = lambda s: ("taglio invisibile" if s >= 0.85 else
                     "dissolvenza a tempo di 1 beat" if s >= 0.7 else "dissolvenza a tempo di 2 beat")
    js = plan["jumps"]
    lines = ["CANZONE ADATTATA: «%s», %.2f s (era %s), importata nel Media Pool e usata "
             "da ora per Marker e Monta." % (name, length, mm(dur))]
    if not js:
        lines.append("• Era già della durata giusta: nessun taglio.")
    elif target < end:
        lines.append("• Tiene l'inizio e il finale veri; toglie %s." % ", ".join(
            "%s–%s" % (mm(a), mm(b)) for a, b, _ in js))
    else:
        a, b, _ = js[0]
        lines.append("• Ripete la parte %s–%s %d volt%s, poi il finale vero." % (
            mm(b), mm(a), len(js), "a" if len(js) == 1 else "e"))
    for a, b, s in js[:1] if target >= end else js:
        lines.append("• Salto %s → %s: somiglianza %.0f%% (%s)." % (mm(a), mm(b), 100 * s, how(s)))
    if not plan["exact"]:
        lines.append("• Durata più vicina possibile tenendo inizio e finale: %.2f s." % length)
    if not feats or not feats[0][0]:
        lines.append("• Confronto senza armonia (manca numpy in Python): meno preciso. "
                     "Installa numpy per salti migliori.")
    lines.append("\nLe clip già analizzate restano. Da ora Analizza e Aggiorna selezione usano "
                 "questa versione finché non selezioni un'altra canzone; per tornare "
                 "all'originale «%s» premi 'usa l'originale'." % old)
    set_log("\n".join(lines))
    set_status("Canzone adattata a %.2f s." % length)


def _preset(key):
    return next((p for p in PRESETS if p[0] == key), None)


def preset_ivs(key):
    return _intervals(BEATS, BPOS, key, _dur(), INTENS, SONG_LEN[0])


def _presets_info():
    """Le tre proposte per la finestra: tagli, quanti stacchi, durata media."""
    if not (BEATS and SHOTS) or STATE["busy"] in ("analyze", "fit", "unfit", "refresh"):
        return []                        # (l'analisi cambia mentre la si legge)
    out = []
    for key, name, desc, ordk in PRESETS:
        ivs = preset_ivs(key)
        L = [b - a for a, b in ivs]
        out.append({"key": key, "name": name, "desc": desc, "ord": ordk.replace("Ord: ", ""),
                    "marks": [round(b, 3) for _, b in ivs],
                    "n": len(L), "avg": sum(L) / len(L) if L else 0.0})
    return out


def _save_marks(marks):
    """Marker in uso (anche per l'editor, che legge quelli salvati)."""
    MARKS[:] = marks
    try:
        os.makedirs(WEB, exist_ok=True)
        with open(os.path.join(WEB, "markers.json"), "w", encoding="utf-8") as f:
            json.dump({"session": SESSION[0], "marks": marks}, f)
    except Exception:
        pass


def on_preset_edit(key):
    """Apre la proposta nell'editor dei marker, per ritoccarla."""
    p = _preset(key)
    if not p or not BEATS:
        set_log("Prima premi 'Analizza'."); return
    SET["ord"] = p[3]                    # l'anteprima e il montaggio usano il suo ordine
    SESSION[0] = "%x" % int(time.time() * 1000)   # un editor gia' aperto non la sovrascrive
    _save_marks([b for _, b in preset_ivs(key)])
    on_mark("Proposta " + p[1])
    set_log("Proposta «%s» aperta nell'editor: %s\n\n%s" % (p[1], p[2], STATE["log"]))


def on_preset_make(key):
    """Monta direttamente la proposta."""
    p = _preset(key)
    if not p or not SHOTS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    msg = montage(SHOTS, SONG, preset_ivs(key), p[1], SET["fmt"], ORDERS[p[3]],
                  SET["reuse"], set_log, SEED[0])
    set_log("%s\nProposta «%s»: %s" % (msg, p[1], p[2]))
    set_status(msg.split("\n")[0][:90])


def on_preset_all(ev=None):
    """Monta tutte e tre le proposte: tre timeline da confrontare in Resolve."""
    if not SHOTS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    msgs = []
    for key, name, desc, ordk in PRESETS:
        set_status("Monto la proposta «%s»…" % name)
        msgs.append(montage(SHOTS, SONG, preset_ivs(key), name, SET["fmt"], ORDERS[ordk],
                            SET["reuse"], set_log, SEED[0]))
    set_log("TRE TIMELINE DA CONFRONTARE in Resolve (BeaTonic_Energia, BeaTonic_Groove, "
            "BeaTonic_Cinematico):\n\n" + "\n\n".join(msgs))
    set_status("Create le tre timeline delle proposte.")


def on_unfit(ev=None):
    """Torna alla canzone originale (la sua analisi e' gia' in memoria)."""
    if not FITTED:
        return
    orig = FITTED["orig_item"]
    FITTED.clear()
    _use_song(orig, analyze=True)
    set_log("Torno alla canzone originale «%s» (%d:%02d)." % (
        orig.GetName(), int(SONG_LEN[0] // 60), int(SONG_LEN[0] % 60)))
    set_status("Canzone originale in uso.")


def on_make(ev):
    if not SHOTS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    style = SET["style"]
    saved = editor_marks()               # i marker salvati dall'editor (come li hai lasciati)
    if saved is not None:
        MARKS[:] = saved
    if MARKS:
        bounds = [0.0] + MARKS
        ivs = list(zip(bounds, bounds[1:]))
        tag, src = "marker", "dai %d marker dell'editor" % len(MARKS)
    else:
        if not BEATS:
            set_log("Prima premi 'Analizza'."); return
        ivs = _intervals(BEATS, BPOS, style, _dur(), INTENS, SONG_LEN[0])
        tag, src = style.split()[0], "dai beat (stile %s)" % style.split()[0]
    set_status("Montaggio in corso…")
    msg = montage(SHOTS, SONG, ivs, tag, SET["fmt"],
                  ORDERS.get(SET["ord"], "nameaz"),
                  SET["reuse"], set_log, SEED[0])
    set_log(msg + "\nStacchi presi " + src + ".")
    set_status(msg.split("\n")[0][:90])


def on_close(ev=None):
    STATE["quit"] = True


CMDS = {"refresh": on_refresh, "analyze": on_analyze, "fit": on_fit, "unfit": on_unfit,
        "mark": on_mark, "make": on_make, "preset_edit": on_preset_edit,
        "preset_make": on_preset_make, "preset_all": on_preset_all, "quit": on_close}

MAIN_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>BeaTonic</title>
<link rel="icon" id="fav" href="/logo">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Rubik+Mono+One&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root { --r: 16px; --gap: 16px; }
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body { font: 14px/1.45 var(--font); color: var(--fg); background: var(--bg); }
.bgfx { position: fixed; inset: 0; z-index: -1; pointer-events: none; background: var(--bgfx); overflow: hidden; }

/* ---------- aspetto STUDIO: scuro, vetro, bagliori viola ---------- */
body.look-studio {
  --font: "Inter", "Segoe UI", system-ui, sans-serif; --hfont: 700 12px "Inter", "Segoe UI", sans-serif;
  --titlefont: 800 30px "Inter", "Segoe UI", sans-serif; --titlels: -.02em; --ctafont: "Inter", "Segoe UI", sans-serif;
  --bg: #0a0a12;
  --bgfx: radial-gradient(900px 500px at 0% -10%, rgba(124,58,237,.38), transparent 60%),
          radial-gradient(800px 500px at 110% 0%, rgba(192,38,211,.22), transparent 60%),
          radial-gradient(900px 600px at 50% 120%, rgba(79,70,229,.25), transparent 60%);
  --fg: #ece9fb; --muted: #a39fc0; --head: #c4b5fd; --logfg: #d6d1f0;
  --card: rgba(255,255,255,.045); --tile: rgba(255,255,255,.04); --seg: rgba(255,255,255,.05);
  --seg-h: rgba(255,255,255,.09); --btn: rgba(255,255,255,.07); --btn-h: rgba(255,255,255,.12);
  --bd: 1px solid rgba(255,255,255,.09); --bd2: 1px solid rgba(255,255,255,.08);
  --shadow: 0 20px 50px rgba(0,0,0,.35); --blur: blur(14px);
  --accent: #8b5cf6; --accent-bg: linear-gradient(135deg, #7c3aed, #c026d3); --onaccent: #fff;
  --onshadow: 0 8px 24px rgba(124,58,237,.45);
  --titlebg: linear-gradient(90deg, #fff, #c4b5fd 60%, #f0abfc); --logofx: drop-shadow(0 6px 18px rgba(192,38,211,.45));
}
/* ---------- aspetto LABS: carta, bordi pieni, ombre nette, tramonto ---------- */
body.look-labs {
  --r: 10px;
  --font: "Inter", "Public Sans", "Segoe UI", sans-serif; --hfont: 400 12px "Rubik Mono One", "Impact", sans-serif;
  --titlefont: 400 30px "Rubik Mono One", "Impact", sans-serif; --titlels: 0; --ctafont: "Rubik Mono One", "Impact", sans-serif;
  --bg: #F3F0F7; --bgfx: radial-gradient(#d8c8ea 1.2px, transparent 1.4px) 0 0 / 18px 18px;
  --fg: #0E0418; --muted: #4B4458; --head: #B0126E; --logfg: #24103F;
  --card: #FBF4FF; --tile: #F3F0F7; --seg: #fff; --seg-h: #F1E8FA; --btn: #fff; --btn-h: #F4F07C;
  --bd: 2.5px solid #0E0418; --bd2: 2px solid #0E0418; --shadow: 6px 6px 0 #0E0418; --blur: none;
  --accent: #EE3A5B; --accent-bg: linear-gradient(90deg, #F4F07C, #F7A541 45%, #EE3A5B); --onaccent: #0E0418;
  --onshadow: 3px 3px 0 #0E0418;
  --titlebg: linear-gradient(180deg, #F7A541, #EE3A5B 55%, #B0126E); --logofx: none;
}
body.look-labs .btn { box-shadow: 3px 3px 0 #0E0418; }
body.look-labs .btn:active, body.look-labs .seg button:active { transform: translate(3px, 3px); box-shadow: none; }
/* ---------- aspetto NOTTE: notte viola, tramonto all'orizzonte, griglia synth ---------- */
body.look-notte {
  --font: "Space Grotesk", "Inter", "Segoe UI", sans-serif; --hfont: 400 12px "Rubik Mono One", "Impact", sans-serif;
  --titlefont: 400 32px "Rubik Mono One", "Impact", sans-serif; --titlels: .02em; --ctafont: "Rubik Mono One", "Impact", sans-serif;
  --bg: #0c0418;
  --bgfx: radial-gradient(760px 380px at 50% 108%, rgba(247,165,65,.55), rgba(238,58,91,.35) 35%, rgba(176,18,110,.18) 60%, transparent 75%),
          linear-gradient(180deg, #12071F 0%, #1d0b36 55%, #2A0B4A 100%);
  --fg: #fbefff; --muted: #c9b3df; --head: #F7A541; --logfg: #f2dcff;
  --card: rgba(20,8,38,.72); --tile: rgba(255,255,255,.04); --seg: rgba(255,255,255,.05);
  --seg-h: rgba(247,165,65,.12); --btn: rgba(255,255,255,.06); --btn-h: rgba(238,58,91,.18);
  --bd: 1px solid rgba(247,165,65,.28); --bd2: 1px solid rgba(255,255,255,.1);
  --shadow: 0 24px 60px rgba(0,0,0,.5), 0 0 40px rgba(176,18,110,.18); --blur: blur(10px);
  --accent: #EE3A5B; --accent-bg: linear-gradient(90deg, #F4F07C, #F7A541 30%, #EE3A5B 65%, #B0126E);
  --onaccent: #12071F; --onshadow: 0 0 24px rgba(238,58,91,.5);
  --titlebg: linear-gradient(180deg, #F4F07C, #F7A541 35%, #EE3A5B 65%, #B0126E);
  --logofx: drop-shadow(0 0 16px rgba(247,165,65,.55));
}
body.look-notte .bgfx::after {
  content: ""; position: absolute; left: -50%; right: -50%; bottom: 0; height: 42%;
  background-image: linear-gradient(rgba(238,58,91,.28) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(238,58,91,.28) 1px, transparent 1px);
  background-size: 64px 42px; transform: perspective(520px) rotateX(62deg); transform-origin: bottom;
  -webkit-mask-image: linear-gradient(transparent, #000 75%); mask-image: linear-gradient(transparent, #000 75%);
}

/* ---------- struttura ---------- */
.top { display: flex; align-items: center; gap: 14px; padding: 18px 24px 6px; max-width: 1280px; margin: 0 auto; }
#logo { height: 48px; width: auto; filter: var(--logofx); }
.brand .name { font: var(--titlefont); letter-spacing: var(--titlels); background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; line-height: 1.05; }
.brand .tag { color: var(--muted); font-size: 12.5px; letter-spacing: .04em; }
.spacer { flex: 1; }
.looks { display: flex; gap: 4px; padding: 4px; border-radius: 999px; background: var(--seg); border: var(--bd2); }
.looks span { align-self: center; padding: 0 6px 0 8px; font-size: 11px; color: var(--muted); }
.looks button { all: unset; cursor: pointer; padding: 6px 12px; border-radius: 999px; font-weight: 700; font-size: 12px; color: var(--muted); }
.looks button.on { background: var(--accent-bg); color: var(--onaccent); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap); padding: 14px 24px 24px; max-width: 1280px; margin: 0 auto; }
.card { background: var(--card); border: var(--bd); border-radius: var(--r); padding: 16px 18px; box-shadow: var(--shadow);
  backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.wide { grid-column: 1 / -1; }
h2 { margin: 0 0 2px; font: var(--hfont); letter-spacing: .12em; text-transform: uppercase; color: var(--head);
  display: flex; align-items: center; gap: 10px; }
h2 .n { display: inline-grid; place-items: center; width: 24px; height: 24px; border-radius: 50%;
  background: var(--accent-bg); color: var(--onaccent); font-size: 12px; letter-spacing: 0; }
label { font-size: 12px; color: var(--muted); font-weight: 600; letter-spacing: .03em; }
.tiles { display: grid; grid-template-columns: 1fr 1.7fr; gap: 10px; }
.tile { background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 4px); padding: 10px 12px; min-width: 0; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
.tile .v { font-size: 30px; font-weight: 800; line-height: 1.15; }
.tile .v.song { font-size: 15px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 6px 0 2px; }
.tile .sub { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.seg { display: flex; flex-wrap: wrap; gap: 6px; }
.seg button { all: unset; cursor: pointer; flex: 1 1 0; min-width: 84px; text-align: center; padding: 8px 10px;
  border-radius: calc(var(--r) - 6px); background: var(--seg); border: var(--bd2); transition: background .15s, transform .1s; }
.seg button b { display: block; font-weight: 700; font-size: 13px; }
.seg button small { display: block; font-size: 11px; color: var(--muted); }
.seg button:hover { background: var(--seg-h); }
.seg button.on { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.seg button.on small { color: inherit; opacity: .8; }
.row { display: grid; grid-template-columns: 1.3fr .8fr .5fr; gap: 14px; align-items: end; }
.row > div { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.dur { display: flex; align-items: center; gap: 8px; font-weight: 700; }
.dur input { width: 100%; font: inherit; font-size: 20px; font-weight: 800; color: var(--fg); background: var(--seg);
  border: var(--bd2); border-radius: calc(var(--r) - 6px); padding: 6px 10px; outline: none; }
.switch { all: unset; cursor: pointer; width: 54px; height: 32px; border-radius: 999px; background: var(--seg); border: var(--bd2); position: relative; display: block; }
.switch i { position: absolute; top: 4px; left: 4px; width: 22px; height: 22px; border-radius: 50%; background: var(--muted); transition: .2s; }
.switch.on { background: var(--accent-bg); }
.switch.on i { left: 26px; background: var(--onaccent); }
.btn { all: unset; cursor: pointer; text-align: center; padding: 11px 16px; border-radius: calc(var(--r) - 4px); font-weight: 700;
  background: var(--btn); border: var(--bd2); transition: background .15s, transform .1s; }
.btn:hover { background: var(--btn-h); }
.btn.primary, .btn.cta { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.btn.cta { font-family: var(--ctafont); font-size: 24px; padding: 24px; letter-spacing: .04em; }
.btn.ghost { padding: 6px 12px; font-size: 12px; }
.btn[disabled] { opacity: .45; pointer-events: none; }
.btn.working { position: relative; overflow: hidden; opacity: 1 !important; }
.btn.working::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255,255,255,.4), transparent); animation: sh 1.1s infinite; }
@keyframes sh { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.hint { margin: -4px 0 4px; font-size: 12px; color: var(--muted); }
.presets { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.preset { background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 4px); padding: 14px;
  display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.pname { font: var(--titlefont); font-size: 21px; line-height: 1.1; background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; }
.pdesc { font-size: 12.5px; color: var(--muted); min-height: 3.9em; }
.strip { width: 100%; height: 30px; border-radius: 6px; border: var(--bd2); display: block; }
.pstats { font-size: 12px; color: var(--muted); }
.pstats b { color: var(--fg); }
.pbtns { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.pbtns .btn { padding: 9px 6px; font-size: 12.5px; }
body.look-studio { --stripA: #f5f3ff; --stripB: #7c3aed; }
body.look-labs { --stripA: #0E0418; --stripB: #ffffff; }
body.look-notte { --stripA: #F7A541; --stripB: #2A0B4A; }
@media (max-width: 860px) { .presets { grid-template-columns: 1fr; } }
.link { all: unset; cursor: pointer; margin-top: 4px; font-size: 12px; font-weight: 700; color: var(--head); }
.link:hover { text-decoration: underline; }
.link[hidden] { display: none; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; min-height: 28px; }
.chip { padding: 4px 10px; border-radius: 999px; background: var(--tile); border: var(--bd2); font-size: 12px; color: var(--muted); }
.chip b { font-size: 14px; color: var(--fg); }
.go { justify-content: center; }
.status { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 0 4px rgba(34,197,94,.18); flex: none; }
body.busy .dot { background: var(--accent); box-shadow: 0 0 0 4px rgba(238,58,91,.2); animation: pulse 1s infinite; }
@keyframes pulse { 50% { opacity: .35; } }
pre#log { margin: 0; max-height: 240px; overflow: auto; white-space: pre-wrap; font: 12.5px/1.55 ui-monospace, Consolas, monospace;
  color: var(--logfg); background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 6px); padding: 12px 14px; }
.overlay { position: fixed; inset: 0; display: grid; place-items: center; background: rgba(0,0,0,.6); z-index: 9; }
.overlay[hidden] { display: none; }
.overlay .card { max-width: 520px; }
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .row { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body class="look-studio" data-look="studio">
<div class="bgfx"></div>
<header class="top">
  <img id="logo" src="/logo" alt="">
  <div class="brand"><div class="name">BeaTonic</div><div class="tag">montaggio a tempo di musica · Plutonic</div></div>
  <div class="spacer"></div>
  <div class="looks" title="Aspetto della finestra (anche l'editor dei marker lo segue)">
    <span>Aspetto</span>
    <button data-look="studio">Studio</button><button data-look="labs">Labs</button><button data-look="notte">Notte</button>
  </div>
</header>
<main class="grid">
  <section class="card">
    <h2><span class="n">1</span> Materiale dal Media Pool</h2>
    <div class="tiles">
      <div class="tile"><div class="k">Clip video</div><div class="v" id="nVid">0</div><div class="sub" id="vidNames">—</div></div>
      <div class="tile"><div class="k">Canzone</div><div class="v song" id="songName">—</div><div class="sub" id="songInfo"></div>
        <button class="link" id="bUnfit" hidden title="Torna alla canzone originale (la sua analisi è già in memoria)">↺ usa l'originale</button></div>
    </div>
    <button class="btn" id="bRefresh" title="Rilegge le clip e la canzone selezionate nel Media Pool">↻ Aggiorna selezione</button>
  </section>
  <section class="card">
    <h2><span class="n">2</span> Analisi</h2>
    <label>Sensibilità scene</label>
    <div class="seg" id="sens"></div>
    <button class="btn primary" id="bAnalyze">Analizza clip e canzone</button>
    <div class="chips" id="chips"></div>
  </section>
  <section class="card wide">
    <h2><span class="n">3</span> Proposte di montaggio</h2>
    <p class="hint">Tre montaggi diversi della stessa canzone. La striscia mostra i tagli lungo la canzone:
      aprili nell'editor per ritoccarli, o montali direttamente (formato e "riusa clip" dal punto 4).</p>
    <div class="presets" id="presets"></div>
    <button class="btn" id="bAll" title="Crea tre timeline in Resolve, una per proposta, da confrontare">▶ Monta tutte e 3 — tre timeline da confrontare in Resolve</button>
  </section>
  <section class="card wide">
    <h2><span class="n">4</span> Oppure personalizza: ritmo e formato</h2>
    <label>Stile dei tagli</label>
    <div class="seg" id="style"></div>
    <div class="row">
      <div><label>Formato</label><div class="seg" id="fmt"></div></div>
      <div><label>Durata del montaggio</label><div class="dur"><input id="dur" type="number" min="1" step="1"><span>s</span></div></div>
      <div><label title="Se il video finisce prima della durata: acceso ricomincia, spento si ferma">Riusa clip</label>
        <button class="switch" id="reuse" role="switch" title="Se il video finisce prima della durata: acceso ricomincia da capo, spento si ferma"><i></i></button></div>
    </div>
    <label>Ordine delle clip</label>
    <div class="seg" id="ord"></div>
  </section>
  <section class="card">
    <h2><span class="n">5</span> Canzone e tagli</h2>
    <button class="btn" id="bFit">♪ Adatta la canzone alla durata</button>
    <p class="hint">Stessa canzone, inizio e finale veri: solo più corta o più lunga, senza tagli udibili.</p>
    <button class="btn" id="bMark">◆ Apri l'editor dei marker</button>
    <p class="hint" id="markInfo">Vedi la canzone e l'anteprima del montato, e sistemi i tagli.</p>
  </section>
  <section class="card go">
    <button class="btn cta" id="bMake">▶ Monta</button>
    <p class="hint" style="text-align:center">Crea la timeline in Resolve: una clip tra un marker e l'altro, sul ritmo.</p>
  </section>
  <section class="card wide">
    <div class="status"><span class="dot"></span><span id="status">Pronto.</span><span class="spacer"></span>
      <button class="btn ghost" id="bQuit" title="Chiude BeaTonic (anche l'editor dei marker)">Chiudi BeaTonic</button></div>
    <pre id="log"></pre>
  </section>
</main>
<div id="off" class="overlay" hidden><div class="card"><h2>BeaTonic è chiuso</h2>
  <p>Per riaprirlo, in DaVinci Resolve: <b>Workspace ▸ Scripts ▸ Utility ▸ BeaTonic</b>.</p></div></div>
<script>
"use strict";
const $ = id => document.getElementById(id);
const BUSY = {refresh: "Leggo la selezione dal Media Pool…", analyze: "Analisi in corso: scene delle clip e beat della canzone…",
  fit: "Adatto la canzone alla durata…", unfit: "Torno alla canzone originale…", mark: "Apro l'editor dei marker…",
  make: "Monto la timeline in Resolve…", quit: "Chiudo…", preset_edit: "Apro la proposta nell'editor…",
  preset_make: "Monto la proposta in Resolve…", preset_all: "Monto le tre proposte in Resolve…"};
const BTN = {refresh: "bRefresh", analyze: "bAnalyze", fit: "bFit", unfit: "bUnfit", mark: "bMark", make: "bMake",
  preset_all: "bAll", quit: "bQuit"};
let presetSig = "", lastLocal = 0;
let ST = null, inited = false, lastLog = null, fails = 0, closing = false;
const esc = s => String(s).replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
const split = s => { const m = /^(.*?)\s*\((.*)\)\s*$/.exec(s); return m ? [m[1], m[2]] : [s, ""]; };
const mmss = t => Math.floor(t / 60) + ":" + String(Math.round(t % 60)).padStart(2, "0");

function seg(id, opts, val, short) {
  const el = $(id);
  el.innerHTML = "";
  for (const o of opts) {
    const b = document.createElement("button"), [t, s] = split(short ? short(o) : o);
    b.innerHTML = "<b>" + esc(t) + "</b>" + (s ? "<small>" + esc(s) + "</small>" : "");
    b.dataset.v = o;
    b.classList.toggle("on", o === val);
    b.onclick = () => { for (const x of el.children) x.classList.toggle("on", x === b); push(); };
    el.appendChild(b);
  }
}
const pick = id => { const b = $(id).querySelector(".on"); return b ? b.dataset.v : ""; };
const collect = () => ({sens: pick("sens"), style: pick("style"), fmt: pick("fmt"), ord: pick("ord"),
  dur: parseFloat($("dur").value) || 30, reuse: $("reuse").classList.contains("on"), look: document.body.dataset.look});
function push() { lastLocal = Date.now(); fetch("/api/set", {method: "POST", body: JSON.stringify(collect())}).catch(() => {}); }
function cmd(c, arg) {
  if (ST && ST.busy) return;
  lastLocal = Date.now();
  fetch("/api/cmd", {method: "POST", body: JSON.stringify({cmd: c, arg: arg, set: collect()})}).catch(() => {});
  if (ST) { ST.busy = c; render(); }
}
function syncControls() {                     // il plugin puo' cambiare le impostazioni (es. ordine di una proposta)
  if (Date.now() - lastLocal < 2000) return;
  for (const k of ["sens", "style", "fmt", "ord"])
    for (const b of $(k).children) b.classList.toggle("on", b.dataset.v === ST.set[k]);
  if (document.activeElement !== $("dur")) $("dur").value = Math.round(ST.set.dur * 100) / 100;
  $("reuse").classList.toggle("on", !!ST.set.reuse);
}
function drawStrip(c, marks) {                // i tagli lungo la canzone: si alternano due colori
  const w = c.clientWidth, h = c.clientHeight, dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext("2d"), cs = getComputedStyle(document.body), end = marks[marks.length - 1] || 1;
  g.scale(dpr, dpr);
  let prev = 0;
  marks.forEach((m, i) => {
    g.fillStyle = cs.getPropertyValue(i % 2 ? "--stripA" : "--stripB").trim();
    g.fillRect(prev / end * w, 0, Math.max(1, (m - prev) / end * w + 0.5), h);
    prev = m;
  });
}
function renderPresets() {
  const ps = ST.presets || [];
  const sig = JSON.stringify(ps.map(p => [p.key, p.n, p.marks[p.marks.length - 1]])) + document.body.dataset.look;
  if (sig !== presetSig) {
    presetSig = sig;
    const box = $("presets");
    box.innerHTML = ps.length ? "" : "<div class='hint'>Analizza clip e canzone per vedere le proposte.</div>";
    for (const p of ps) {
      const el = document.createElement("div");
      el.className = "preset";
      el.innerHTML = "<div class='pname'>" + esc(p.name) + "</div><div class='pdesc'>" + esc(p.desc) + "</div>" +
        "<canvas class='strip'></canvas><div class='pstats'><b>" + p.n + "</b> stacchi · media <b>" +
        p.avg.toFixed(1).replace(".", ",") + " s</b> · " + esc(p.ord.toLowerCase()) + "</div>" +
        "<div class='pbtns'><button class='btn' data-c='preset_edit'>◆ Apri nell'editor</button>" +
        "<button class='btn primary' data-c='preset_make'>▶ Monta questa</button></div>";
      for (const b of el.querySelectorAll("button")) b.onclick = () => cmd(b.dataset.c, p.key);
      box.appendChild(el);
      drawStrip(el.querySelector("canvas"), p.marks);
    }
  }
  for (const b of $("presets").querySelectorAll("button")) b.disabled = !!ST.busy;
}
window.addEventListener("resize", () => { presetSig = ""; if (ST) renderPresets(); });
function setLook(l) {
  document.body.className = "look-" + l;
  document.body.dataset.look = l;
  for (const b of document.querySelectorAll(".looks button")) b.classList.toggle("on", b.dataset.look === l);
}
function favicon() {                          // logo Plutonic quadrato: icona della finestra e della barra
  const im = new Image();
  im.onload = () => {
    const c = document.createElement("canvas"), g = c.getContext("2d");
    c.width = c.height = 128;
    const s = Math.min(128 / im.width, 128 / im.height);
    g.drawImage(im, (128 - im.width * s) / 2, (128 - im.height * s) / 2, im.width * s, im.height * s);
    $("fav").href = c.toDataURL("image/png");
  };
  im.src = "/logo";
}
function init() {
  inited = true;
  const s = ST.set, o = ST.opts;
  seg("sens", o.sens, s.sens);
  seg("style", o.style, s.style);
  seg("fmt", o.fmt, s.fmt, x => { const m = /^(\S+)\s+(\d+:\d+)$/.exec(x); return m ? m[2] + " (" + m[1].toLowerCase() + ")" : x; });
  seg("ord", o.ord, s.ord, x => x.replace(/^Ord:\s*/, ""));
  $("dur").value = Math.round(s.dur * 100) / 100;
  $("reuse").classList.toggle("on", !!s.reuse);
  setLook(s.look || "studio");
}
function render() {
  const v = ST.sel.videos || [], inf = ST.info;
  $("nVid").textContent = v.length;
  $("vidNames").textContent = v.length ? v.slice(0, 3).join(" · ") + (v.length > 3 ? " +" + (v.length - 3) : "") : "selezionale nel Media Pool";
  $("songName").textContent = ST.sel.song || "—";
  $("songInfo").textContent = ST.sel.song ? (inf.song_len ? mmss(inf.song_len) + (inf.fitted ? " · adattata · originale: " + inf.orig : "")
                                                          : "da analizzare")
                                          : "selezionala nel Media Pool";
  $("bUnfit").hidden = !inf.fitted;
  $("chips").innerHTML = inf.analyzed ?
    ["<b>" + inf.shots + "</b> pezzi video", "<b>" + Math.round(inf.bpm) + "</b> BPM", "<b>" + inf.bars + "</b> battute", "<b>" + inf.beats + "</b> beat"]
      .map(x => "<span class='chip'>" + x + "</span>").join("") : "<span class='chip'>non ancora analizzato</span>";
  $("markInfo").textContent = inf.marks ? inf.marks + " marker pronti: si salvano da soli dall'editor."
                                        : "Vedi la canzone e l'anteprima del montato, e sistemi i tagli.";
  if (ST.log !== lastLog) { const L = $("log"); L.textContent = ST.log; L.scrollTop = 0; lastLog = ST.log; }
  $("status").textContent = ST.busy ? (BUSY[ST.busy] || "Lavoro…") : ST.status;
  document.body.classList.toggle("busy", !!ST.busy);
  for (const [c, id] of Object.entries(BTN)) {
    $(id).disabled = !!ST.busy && c !== ST.busy;
    $(id).classList.toggle("working", ST.busy === c);
  }
  for (const id of ["bAnalyze"]) if (!ST.busy) $(id).disabled = !(v.length && ST.sel.song);
  for (const id of ["bFit", "bMark", "bMake", "bAll"]) if (!ST.busy) $(id).disabled = !inf.analyzed;
  renderPresets();
  syncControls();
}
async function poll() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    ST = await r.json();
    fails = 0; $("off").hidden = true;
    if (!inited) init();
    render();
    if (ST.quit) { closing = true; $("off").hidden = false; setTimeout(() => window.close(), 400); return; }
  } catch (e) { if (++fails > 3) $("off").hidden = false; }
  setTimeout(poll, ST && ST.busy ? 300 : 700);
}
for (const b of document.querySelectorAll(".looks button")) b.onclick = () => { setLook(b.dataset.look); push(); };
$("reuse").onclick = () => { $("reuse").classList.toggle("on"); push(); };
$("dur").addEventListener("change", push);
for (const [c, id] of Object.entries(BTN)) $(id).onclick = () => cmd(c);
favicon();
poll();
</script>
</body>
</html>
"""


def main():
    """Avvia il server, apre il pannello ed esegue i comandi finche' c'e' almeno una
    finestra di BeaTonic aperta (pannello o editor): poi si chiude da solo."""
    os.makedirs(WEB, exist_ok=True)
    _load_settings()
    try:
        on_refresh()
    except Exception:
        pass
    set_log("1) Seleziona nel Media Pool le clip video + la canzone   2) Analizza\n"
            "3) Scegli stile, formato, durata e ordine   4) Facoltativi: adatta la canzone, "
            "editor dei marker   5) Monta.")
    port = start_server()
    print("BeaTonic: pannello su http://127.0.0.1:%d/" % port)
    _open_window(_url(), (1180, 860))
    started = time.time()
    while not STATE["quit"]:
        try:
            cmd, arg = CMDQ.get(timeout=0.25)
        except queue.Empty:
            cmd, arg = None, None
        if cmd in CMDS:
            STATE["busy"] = cmd
            try:
                CMDS[cmd](arg)
            except Exception as e:      # mai piu' finestre bloccate: l'errore va nel log
                import traceback
                set_log("ERRORE durante '%s':\n%s" % (cmd, traceback.format_exc()[-1800:]))
                set_status("Errore: %s" % e)
            finally:
                STATE["busy"] = ""
        last = STATE["pinged"] or started
        if time.time() - last > (25 if STATE["pinged"] else 180):
            break                        # nessuna finestra aperta da un po': fine
    time.sleep(1.0)                      # la finestra fa in tempo a vedere "chiuso"
    try:
        SERVER["srv"].shutdown()
    except Exception:
        pass


main()
