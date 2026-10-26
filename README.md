# BeatCut

Plugin per DaVinci Resolve che crea un **montaggio ritmato a tempo di musica**:
analizza i **cambi di scena** nelle clip (le spezza in "shot") e i **beat** della
canzone, poi monta le shot tagliate **sui beat**.

Richiede **ffmpeg** (rilevato in automatico in `D:\ffmpeg\bin`, o imposta la variabile
d'ambiente `FFMPEG` col percorso). Self-contained, zero dipendenze Python.

## Uso
Installa `resolve_scripts/BeatCut.py` in
`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`.
Da **Workspace ▸ Scripts ▸ Utility ▸ BeatCut**:

1. Nel **Media Pool** seleziona le **clip video** (sorgenti) **+ la canzone** (clip audio).
2. **Aggiorna selezione** → controlla che veda N clip + la canzone.
3. **Analizza**: ffmpeg rileva i cambi di scena (sensibilità regolabile) e i beat
   (BPM + griglia). *(Operazione lenta: la finestra resta ferma.)*
4. Scegli **stile** (veloce/medio/lento/dinamico), **formato** (orizzontale 16:9 /
   verticale 9:16), **durata** (secondi), e se vuoi l'ordine **casuale**.
5. **Monta a tempo** → crea una timeline con la canzone su A1 e le shot sui beat.

## Note
- Lo scene-cut e il beat-detect li fa **ffmpeg + DSP in Python** (Resolve non li espone).
- Il beat detector è "buono", non da software musicale pro: ottimo su brani ritmici.
- Il verticale da sorgenti orizzontali ritaglia/adatta secondo le impostazioni di conforma.
