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


def _decode_mono(path, sr=11025, af=None, name="_song.wav"):
    out = os.path.join(_TMP, name)
    cmd = [FFMPEG, "-y", "-i", path, "-ac", "1", "-ar", str(sr)]
    if af:
        cmd += ["-af", af]
    subprocess.run(cmd + [out], capture_output=True, creationflags=_NOWIN)
    w = wave.open(out, "rb")
    raw = w.readframes(w.getnframes())
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


MIN_SCENE = 1.5   # s: cambi scena piu' vicini di cosi' sono quasi sempre falsi
                  # (movimento veloce, flash, luce che cambia) e vengono ignorati


def build_shots(videos, thresh, split, set_log):
    """Spezza ogni clip ai cambi di scena -> lista di shot {clip,fps,in,out}.
    Con split=False ogni clip resta UNA shot intera (niente scene detection)."""
    shots = []
    for i, c in enumerate(videos, 1):
        path = c.GetClipProperty("File Path") or ""
        fps = _clip_fps(c)
        dur = _clip_dur_sec(c)
        if not split or not path:
            if dur > 0.1:
                shots.append({"clip": c, "fps": fps, "in": 0.0, "out": dur})
            continue
        set_log("Analisi scene %d/%d: %s…" % (i, len(videos), c.GetName()))
        bounds = [0.0]
        for t in scene_cuts(path, thresh):
            if t - bounds[-1] >= MIN_SCENE and dur - t >= MIN_SCENE:
                bounds.append(t)
        bounds.append(dur)
        for a, b in zip(bounds, bounds[1:]):
            shots.append({"clip": c, "fps": fps, "in": a, "out": b})
    return shots


# --------------------------------------------------------------------------
# Montaggio (tape continuo di shot, tagliato sui beat)
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
    """Punti di stacco scelti SULLA GRIGLIA MUSICALE: beat, mezze battute o battute."""
    mode = STYLES.get(style, "beat")
    barv = [0.5] * len(beats)
    if mode == "dyn":                 # energia di ogni battuta = media dei suoi beat
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
    pts = []
    for i, t in enumerate(beats):
        p = bpos[i]
        if mode == "beat":
            keep = True
        elif mode == "half":
            keep = p in (0, 2)
        elif mode == "bar":
            keep = p == 0
        else:                         # dinamico: fitto se spinge, largo se calmo
            v = barv[i]
            keep = True if v >= 0.6 else (p in (0, 2) if v >= 0.33 else p == 0)
        if keep:
            pts.append(t)
    return pts


def _intervals(beats, bpos, style, dur, intens):
    """Intervalli (inizio, fine) del montaggio: partono da 0 (come la canzone in
    timeline), mai piu' corti di MIN_CUT, e finiscono sull'inizio di una battuta."""
    pts, last = [], 0.0
    for t in _cut_points(beats, bpos, style, intens):
        if t - last >= MIN_CUT and t <= dur + 0.01:
            pts.append(t)
            last = t
    if not pts:
        return []
    downs = set(t for t, p in zip(beats, bpos) if p == 0)
    for k in range(len(pts) - 1, max(-1, len(pts) - 9), -1):
        if pts[k] in downs:           # chiudi sul "1" piu' vicino alla durata scelta
            pts = pts[:k + 1]
            break
    bounds = [0.0] + pts
    return list(zip(bounds, bounds[1:]))


ORDERS = {
    "Ord: Nome A→Z": "nameaz",
    "Ord: Nome Z→A": "nameza",
    "Ord: Selezione": "selezione",
    "Ord: Casuale (mischia tutto)": "casuale",
}


def order_shots(shots, mode):
    """Ordine delle shot nel montaggio. Per nome: le shot dello stesso video restano
    insieme e in ordine di tempo. Casuale: mischia le shot di TUTTI i video."""
    tape = list(shots)
    if mode == "casuale":
        import random
        random.shuffle(tape)
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


def marker_timeline(song, ivs, beats, bpos, tag):
    """Timeline con la sola canzone (intera) e un marker a ogni punto di stacco:
    rosso = inizio battuta, ciano = beat. Si sistemano a mano prima di montare."""
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()
    new = _new_timeline(mp, "BeaTonic_marker_%s" % tag)
    if new is None:
        return None, 0
    proj.SetCurrentTimeline(new)
    fps = _tl_fps(new)
    sec = _clip_dur_sec(song)
    _put_song(mp, song, new.GetStartFrame(), int(round(sec * fps)) if sec else None)
    downs = set(t for t, p in zip(beats, bpos) if p == 0)
    n, used = 0, set()
    for _, b in ivs:
        f = int(round(b * fps))
        if f <= 0 or f in used:
            continue
        used.add(f)
        one = b in downs
        try:
            if new.AddMarker(f, "Red" if one else "Cyan", "1" if one else "beat",
                             "BeaTonic: punto di stacco", 1):
                n += 1
        except Exception:
            pass
    return new, n


def find_timeline(uid):
    proj = resolve.GetProjectManager().GetCurrentProject()
    for i in range(1, int(proj.GetTimelineCount() or 0) + 1):
        t = proj.GetTimelineByIndex(i)
        try:
            if t and (t.GetUniqueId() == uid or t.GetName() == uid):
                return t
        except Exception:
            pass
    return None


def marker_cuts(tl):
    """Punti di stacco = TUTTI i marker della timeline, anche quelli aggiunti a mano,
    piu' quelli messi sulla clip della canzone (M con la clip selezionata).
    Ritorna ([frame dall'inizio, in ordine], {frame: info marker})."""
    marks = {}
    try:
        for f, info in (tl.GetMarkers() or {}).items():
            marks[int(round(float(f)))] = info
    except Exception:
        pass
    ns = tl.GetStartFrame()
    try:
        for it in tl.GetItemListInTrack("audio", 1) or []:
            off = it.GetStart() - ns
            for f, info in (it.GetMarkers() or {}).items():
                marks.setdefault(off + int(round(float(f))), info)
    except Exception:
        pass
    return sorted(f for f in marks if f > 0), marks


def montage(shots, song, ivs, tag, fmt, order_mode, reuse, set_log, marks=None):
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
    tape = order_shots(shots, order_mode)

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

    ok = 0
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
                try:
                    cur = items[0].GetStart() + items[0].GetDuration()
                except Exception:
                    cur += plen
        if exhausted[0]:
            break
    frames = cur - ns
    _put_song(mp, song, ns, frames)             # canzone su A1, lunga quanto il montaggio
    for f, info in sorted((marks or {}).items()):
        if 0 < f <= frames:                     # i marker restano anche nel montato
            try:
                new.AddMarker(f, info.get("color", "Blue"), info.get("name", ""),
                              info.get("note", ""), 1)
            except Exception:
                pass
    return ("Montaggio '%s' creato: %d stacchi su %.1fs, %s."
            % (new.GetName(), ok, frames / tl_fps, fmt))


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
QPushButton#Logo { background:transparent; border:0; margin:0px; }
QPushButton#ThemeBtn { background:transparent; border:%(bw)s solid %(border2)s; border-radius:%(radius)s; padding:4px 9px; color:%(muted)s; font-weight:700; }
QPushButton#ThemeBtn:hover { border:%(bw)s solid %(accent)s; color:%(fg)s; }
QLabel#Src { color:%(muted)s; padding:4px 2px; }
QLineEdit, QComboBox { background:%(panel)s; border:%(bw)s solid %(border)s; border-radius:%(radius)s; padding:6px 9px; margin:0px 3px; selection-background-color:%(accent)s; selection-color:%(selfg)s; }
QLineEdit:focus, QComboBox:focus { border:%(bw)s solid %(focus)s; }
QComboBox::drop-down { border:0; width:18px; }
QPushButton { background:%(panel)s; border:%(bw)s solid %(border2)s; border-radius:%(radius)s; padding:7px 13px; margin:0px 3px; }
QPushButton:hover { background:%(border)s; }
QPushButton:pressed { background:%(tree)s; }
QPushButton#Analyze, QPushButton#Make { background:%(primaryBg)s; color:%(primaryFg)s; font-weight:700; %(primaryBorder)s padding:9px 13px; }
QPushButton#Analyze:hover, QPushButton#Make:hover { background:%(primaryHover)s; }
QPushButton#Close { color:%(muted)s; }
QCheckBox { spacing:6px; margin:0px 3px; }
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
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.Button({"ID": "Logo", "Flat": True, "Weight": 0,
                       "MinimumSize": [62, 48], "MaximumSize": [62, 48]}),
            ui.Label({"ID": "Header", "Text": "BeaTonic", "Weight": 0}),
            ui.Label({"Text": "montaggio a tempo di musica", "Weight": 1,
                      "Alignment": {"AlignBottom": True}}),
            ui.Button({"ID": "ThemeBtn", "Text": "☀  Tema Labs", "Weight": 0}),
        ]),
        ui.Label({"ID": "Src", "Text": "Seleziona clip + canzone nel Media Pool, poi Aggiorna.",
                  "Weight": 0}),

        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.Button({"ID": "Refresh", "Text": "Aggiorna selezione", "Weight": 4}),
            ui.Label({"Text": "Sensib. scene", "Weight": 2}),
            ui.ComboBox({"ID": "Sens", "Weight": 3}),
            ui.Button({"ID": "Analyze", "Text": "▶  Analizza", "Weight": 4}),
        ]),

        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.ComboBox({"ID": "Style", "Weight": 5}),
            ui.ComboBox({"ID": "Fmt", "Weight": 4}),
            ui.Label({"Text": "Durata s", "Weight": 1}),
            ui.LineEdit({"ID": "Dur", "PlaceholderText": "30", "Weight": 1}),
            ui.ComboBox({"ID": "Ord", "Weight": 5}),
            ui.CheckBox({"ID": "Reuse", "Text": "riusa clip", "Checked": True, "Weight": 2}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.Button({"ID": "Mark", "Text": "◆  Marker sui beat", "Weight": 3}),
            ui.Button({"ID": "Make", "Text": "▶  Monta", "Weight": 3}),
            ui.Button({"ID": "Close", "Text": "Chiudi", "Weight": 1}),
        ]),

        ui.TextEdit({"ID": "Log", "Weight": 1, "ReadOnly": True,
                     "Text": "1) Seleziona clip + canzone  2) Analizza  "
                             "3) Marker sui beat: controlli e sistemi i tagli (facoltativo)  "
                             "4) Monta."}),
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
for c in ["0.4 (consigliata)", "0.3 (media)", "0.2 (sensibile)", "0.6 (minima)",
          "Non dividere (clip intere)"]:
    itm["Sens"].AddItem(c)
for s in STYLES:
    itm["Style"].AddItem(s)
for f in FORMATS:
    itm["Fmt"].AddItem(f)
for o in ORDERS:
    itm["Ord"].AddItem(o)

VIDEOS, SONG, SHOTS, BEATS, BPOS, BPM, INTENS = [], None, [], [], [], 0.0, []
MARK_UID = None        # timeline dei marker creata con "Marker sui beat"


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
    global SHOTS, BEATS, BPOS, BPM, INTENS, MARK_UID
    MARK_UID = None                      # nuova analisi: i marker vecchi non valgono piu'
    on_refresh()
    if not FFMPEG:
        set_log("ffmpeg non trovato: impossibile analizzare."); return
    if not VIDEOS:
        set_log("Nessuna clip video selezionata nel Media Pool."); return
    if not SONG:
        set_log("Nessuna canzone (clip audio) selezionata nel Media Pool."); return
    sens_txt = itm["Sens"].CurrentText or "0.4"
    split = not sens_txt.startswith("Non")
    sens = float(sens_txt.split()[0]) if split else 0.4
    set_status("Analisi in corso (la finestra resta ferma)…")
    SHOTS = build_shots(VIDEOS, sens, split, set_log)
    set_log("Scene analizzate: %d shot totali.\nAnalizzo i beat della canzone…" % len(SHOTS))
    BPM, BEATS, BPOS, INTENS = beat_grid(SONG.GetClipProperty("File Path") or "")
    bars = sum(1 for p in BPOS if p == 0)
    set_log("ANALISI FATTA.\n• %d shot da %d clip (%s).\n• Canzone: %.0f BPM, %d beat, "
            "%d battute (stacco minimo %.2fs).\n\n"
            "Ora scegli stile/durata/formato/ordine. Premi 'Marker sui beat' per vedere "
            "e sistemare i tagli sulla canzone, oppure direttamente 'Monta'."
            % (len(SHOTS), len(VIDEOS), "divise ai cambi scena" if split else "intere",
               BPM, len(BEATS), bars, MIN_CUT))
    set_status("Analisi ok: %d shot · %.0f BPM · %d beat." % (len(SHOTS), BPM, len(BEATS)))


def _dur():
    try:
        return float((itm["Dur"].Text or "30").replace(",", "."))
    except ValueError:
        return 30.0


def on_mark(ev):
    """Timeline con la canzone e i marker sui punti di stacco, da controllare."""
    global MARK_UID
    if not BEATS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    style = itm["Style"].CurrentText
    ivs = _intervals(BEATS, BPOS, style, _dur(), INTENS)
    if not ivs:
        set_log("Nessun punto di stacco: controlla la canzone o la durata."); return
    tl, n = marker_timeline(SONG, ivs, BEATS, BPOS, style.split()[0])
    if tl is None:
        set_log("Impossibile creare la timeline dei marker."); return
    try:
        MARK_UID = tl.GetUniqueId()
    except Exception:
        MARK_UID = tl.GetName()
    set_log("TIMELINE '%s': canzone + %d marker (stile %s, %.0fs).\n\n"
            "Ascoltala e sistema i tagli come vuoi:\n"
            "• ROSSO = inizio battuta, CIANO = beat\n"
            "• spostare: trascina il marker sul righello\n"
            "• togliere: clic sul marker e Canc\n"
            "• aggiungere: metti la testina dove vuoi e premi M\n"
            "L'ultimo marker e' la FINE del montaggio.\n\n"
            "Quando ti va bene premi 'Monta': mette una clip tra un marker e l'altro."
            % (tl.GetName(), n, style.split()[0], _dur()))
    set_status("Marker pronti: %d. Sistemali, poi 'Monta'." % n)


def on_make(ev):
    if not SHOTS or not SONG:
        set_log("Prima premi 'Analizza'."); return
    style = itm["Style"].CurrentText
    marks = None
    mk = find_timeline(MARK_UID) if MARK_UID else None
    if mk is not None:                   # i marker (come li hai lasciati) comandano
        cuts, marks = marker_cuts(mk)
        if not cuts:
            set_log("La timeline '%s' non ha marker: aggiungine col tasto M oppure "
                    "ricreali con 'Marker sui beat'." % mk.GetName()); return
        fps = _tl_fps(mk)
        bounds = [0] + cuts
        ivs = [(a / fps, b / fps) for a, b in zip(bounds, bounds[1:])]
        tag, src = "marker", "dai %d marker di '%s'" % (len(cuts), mk.GetName())
    else:
        if not BEATS:
            set_log("Prima premi 'Analizza'."); return
        ivs = _intervals(BEATS, BPOS, style, _dur(), INTENS)
        tag, src = style.split()[0], "dai beat (stile %s)" % style.split()[0]
    set_status("Montaggio in corso…")
    msg = montage(SHOTS, SONG, ivs, tag, itm["Fmt"].CurrentText,
                  ORDERS.get(itm["Ord"].CurrentText, "nameaz"),
                  bool(itm["Reuse"].Checked), set_log, marks)
    set_log(msg + "\nStacchi presi " + src + ".")
    set_status(msg[:80])


def on_close(ev):
    disp.ExitLoop()


win.On.Refresh.Clicked = on_refresh
win.On.Analyze.Clicked = on_analyze
win.On.Mark.Clicked = on_mark
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
