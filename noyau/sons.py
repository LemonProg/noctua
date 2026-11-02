"""Bruitages des reels, synthétisés avec la Web Audio API (aucun fichier son extérieur, aucun droit d'auteur).

Claude écrit chaque bruitage comme le corps d'une fonction `son(ctx, aleatoire)` : `ctx` est un
OfflineAudioContext (stéréo, 48 kHz, de la durée du son), `aleatoire()` un générateur à graine fixe (même son
à chaque rendu). Chrome sans fenêtre calcule le son hors ligne et le renvoie en WAV, placé ensuite dans la vidéo.

Page verrouillée comme pour les visuels : aucun accès réseau (politique de sécurité du contenu, résolution DNS
coupée), pas de Math.random ni de requête dans le code du son. Un code en erreur, un son muet ou trop long est
refusé avec un message, renvoyé à Claude pour correction.
"""

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

from noyau.rendu import trouver_chromium

FREQUENCE = 48000
DUREE_MAX = 5.0
LONGUEUR_CODE_MAX = 4000
DELAI_SECONDES = 30
INTERDITS = re.compile(r"Math\.random|fetch\s*\(|XMLHttpRequest|import\s*\(|WebSocket|<\s*/?\s*script", re.I)
SORTIE = re.compile(r'<pre id="sortie">(WAV:[A-Za-z0-9+/=]+|ERREUR:.*?)</pre>', re.S)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'">
</head><body><pre id="sortie">EN ATTENTE</pre><script>
function wav(buffer) {{
  const n = buffer.length, canaux = buffer.numberOfChannels, sr = buffer.sampleRate;
  const octets = new DataView(new ArrayBuffer(44 + n * canaux * 2));
  const texte = (o, s) => [...s].forEach((c, i) => octets.setUint8(o + i, c.charCodeAt(0)));
  texte(0, "RIFF"); octets.setUint32(4, 36 + n * canaux * 2, true); texte(8, "WAVEfmt ");
  octets.setUint32(16, 16, true); octets.setUint16(20, 1, true); octets.setUint16(22, canaux, true);
  octets.setUint32(24, sr, true); octets.setUint32(28, sr * canaux * 2, true); octets.setUint16(32, canaux * 2, true);
  octets.setUint16(34, 16, true); texte(36, "data"); octets.setUint32(40, n * canaux * 2, true);
  let crete = 0;
  for (let i = 0; i < n; i++) for (let c = 0; c < canaux; c++) {{
    const v = Math.max(-1, Math.min(1, buffer.getChannelData(c)[i]));
    crete = Math.max(crete, Math.abs(v));
    octets.setInt16(44 + (i * canaux + c) * 2, v * 32767, true);
  }}
  if (crete < 0.01) throw new Error("son muet (rien n'est relié à ctx.destination, ou gain nul)");
  let binaire = ""; const b = new Uint8Array(octets.buffer);
  for (let i = 0; i < b.length; i += 8192) binaire += String.fromCharCode(...b.subarray(i, i + 8192));
  return btoa(binaire);
}}
const sortie = document.getElementById("sortie");
try {{
  let graine = 20261030;
  const aleatoire = () => (graine = (graine * 16807) % 2147483647) / 2147483647;
  const ctx = new OfflineAudioContext(2, Math.ceil({frequence} * {duree}), {frequence});
  new Function("ctx", "aleatoire", {code})(ctx, aleatoire);  // erreur de syntaxe : message exact
  ctx.startRendering().then(b => {{ sortie.textContent = "WAV:" + wav(b); }})
    .catch(e => {{ sortie.textContent = "ERREUR:" + e.message; }});
}} catch (e) {{ sortie.textContent = "ERREUR:" + e.message; }}
</script></body></html>
"""


class SonErreur(Exception):
    """Code de bruitage refusé ou en erreur : à corriger par Claude."""


def synthetiser(code: str, duree: float, cible: Path, dossier: Path, chromium: str | None = None) -> Path:
    """Calcule le son décrit par `code` (Web Audio) et l'écrit en WAV dans `cible`."""
    if not 0.05 <= duree <= DUREE_MAX:
        raise SonErreur(f"durée de {duree:g} s : entre 0,05 et {DUREE_MAX:g} s")
    if len(code) > LONGUEUR_CODE_MAX:
        raise SonErreur(f"code de {len(code)} caractères : {LONGUEUR_CODE_MAX} au plus")
    interdit = INTERDITS.search(code)
    if interdit:
        raise SonErreur(f"« {interdit.group(0)} » interdit (utiliser aleatoire() pour le hasard ; aucune requête)")
    page = dossier / f"{cible.stem}.html"
    page.write_text(PAGE.format(code=json.dumps(code).replace("</", "<\\/"), duree=f"{duree:g}",
                                frequence=FREQUENCE), encoding="utf-8")
    profil = dossier / f"profil-{cible.stem}"
    commande = [chromium or trouver_chromium(), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", "--mute-audio",
                "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--user-data-dir={profil}", "--virtual-time-budget=10000", "--dump-dom", page.as_uri()]
    try:
        resultat = subprocess.run(commande, capture_output=True, text=True, timeout=DELAI_SECONDES)
    except subprocess.TimeoutExpired:
        raise SonErreur(f"calcul du son trop long (plus de {DELAI_SECONDES} s)") from None
    finally:
        shutil.rmtree(profil, ignore_errors=True)
        page.unlink(missing_ok=True)
    trouve = SORTIE.search(resultat.stdout)
    if not trouve:
        raise SonErreur("le code n'a produit aucun son (erreur de syntaxe probable)")
    if trouve.group(1).startswith("ERREUR:"):
        raise SonErreur(trouve.group(1)[len("ERREUR:"):].strip())
    cible.write_bytes(base64.b64decode(trouve.group(1)[len("WAV:"):]))
    return cible


EXEMPLES = json.dumps({
    "whoosh (0,6 s)": "const b = ctx.createBuffer(1, ctx.length, ctx.sampleRate); const d = b.getChannelData(0);\n"
                      "for (let i = 0; i < d.length; i++) d[i] = aleatoire() * 2 - 1;\n"
                      "const s = ctx.createBufferSource(); s.buffer = b; const f = ctx.createBiquadFilter();\n"
                      "f.type = 'bandpass'; f.frequency.setValueAtTime(300, 0); f.frequency.exponentialRampToValueAtTime(4000, 0.45);\n"
                      "const g = ctx.createGain(); g.gain.setValueAtTime(0, 0); g.gain.linearRampToValueAtTime(0.6, 0.25);\n"
                      "g.gain.linearRampToValueAtTime(0, 0.6); s.connect(f).connect(g).connect(ctx.destination); s.start(0);",
    "pop (0,15 s)": "const o = ctx.createOscillator(); o.frequency.setValueAtTime(900, 0); o.frequency.exponentialRampToValueAtTime(300, 0.12);\n"
                    "const g = ctx.createGain(); g.gain.setValueAtTime(0.7, 0); g.gain.exponentialRampToValueAtTime(0.001, 0.15);\n"
                    "o.connect(g).connect(ctx.destination); o.start(0); o.stop(0.15);",
}, ensure_ascii=False, indent=1)
