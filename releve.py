#!/usr/bin/env python3
"""Relevé public des cours spot or et argent (BullionVault) -> site/data/public.json

Projet autonome : aucune donnée confidentielle, aucun secret. Écrit les cours au kilo,
la variation de la semaine (donnée par BullionVault) et la variation depuis la veille
(calculée à partir de l'historique conservé dans le fichier publié).

Usage : python3 releve.py site/data/public.json
"""
import datetime as dt
import json
import os
import re
import sys
import unicodedata
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

URL_OR = "https://or.bullionvault.fr/Cours-De-L-Or.do"
URL_ARGENT = "https://or.bullionvault.fr/Cours-De-L-Argent-Metal.do"
OZ_G = 31.1034768


# ---------------------------------------------------------------- utilitaires
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def parse_eur(s):
    """'1 240,00 €' / '1 250.77 €' / '99.73' -> float (None si illisible)."""
    if s is None:
        return None
    s = re.sub(r"[\s  €]", "", str(s))
    m = re.search(r"-?\d[\d.,]*", s)
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ analyse
def parse_bullionvault(text):
    """Renvoie (euros par kilo, horodatage texte) depuis le texte de la page."""
    t = re.sub(r"[  ]", " ", text or "")
    m = re.search(r"par\s+kilo(?:gramme)?\D{0,40}?(\d[\d ]*(?:[.,]\d+)?)\s*€", t, re.I)
    kg = parse_eur(m.group(1)) if m else None
    if kg is None:
        m = re.search(r"par\s+once\D{0,40}?(\d[\d ]*(?:[.,]\d+)?)\s*€", t, re.I)
        if m:
            kg = parse_eur(m.group(1)) * 1000 / OZ_G
    when = re.search(r"(\d{1,2}\s+\w+\s+20\d\d,?\s+\d{1,2}:\d{2}:\d{2}[^|\n]*)", t)
    return kg, (when.group(1).strip() if when else None)


def parse_variation_semaine(text):
    """Variation sur une semaine affichée par BullionVault : « Une Semaine Variation | -9 375,45 € (8,29 %) ».
    Renvoie {'eur': -9375.45, 'pct': -8.29} (le signe du pourcentage est celui du montant) ou None."""
    t = re.sub(r"[\u00a0\u202f]", " ", text or "")
    m = re.search(r"Une\s+Semaine\s+Variation\D{0,120}?([-−+]?)\s*(\d[\d ]*(?:[.,]\d+)?)\s*€\s*\(\s*([-−+]?)\s*(\d+(?:[.,]\d+)?)\s*%", t, re.I)
    if not m:
        return None
    eur = parse_eur(m.group(2)); pct = parse_eur(m.group(4))
    if eur is None or pct is None:
        return None
    neg = (m.group(1) or m.group(3)) in ("-", "−")
    sg = -1 if neg else 1
    return {"eur": sg * eur, "pct": sg * pct}


# --------------------------------------------------------------- navigateur
class Navigateur:
    """Petit enrobage de Playwright : renvoie (html, texte) d'une page rendue."""

    def __enter__(self):
        if sync_playwright is None:
            raise RuntimeError("Playwright n'est pas installé")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._ctx = self._browser.new_context(
            locale="fr-FR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        return self

    def __exit__(self, *a):
        self._browser.close()
        self._pw.stop()

    def page(self, url, attendre_texte=None, timeout=30000, bonus=None):
        p = self._ctx.new_page()
        try:
            p.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                p.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            if attendre_texte:
                p.wait_for_function(
                    "t => new RegExp(t, 'i').test(document.body.innerText)",
                    arg=attendre_texte, timeout=timeout)
            texte = p.inner_text("body")
            if bonus and not re.search(bonus, texte, re.I):
                # contenu chargé en retard ou masqué (onglet) : on attend un peu, puis on lit aussi le texte caché
                try:
                    p.wait_for_function("t => new RegExp(t, 'i').test(document.body.textContent)", arg=bonus, timeout=12000)
                except Exception:
                    pass
                texte += "\n" + re.sub(r"\s+", " ", p.evaluate("document.body.textContent") or "")
            return p.content(), texte
        finally:
            p.close()


# ------------------------------------------------------------------- relevé
def precedent():
    """Dernier fichier publié (pour l'historique et, en secours, les derniers cours connus)."""
    try:
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
        url = f"https://{owner.lower()}.github.io/{repo}/data/public.json?t={os.getpid()}"
        req = urllib.request.Request(url, headers={"User-Agent": "cours-metaux"})
        return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:  # noqa: BLE001
        print("pas de fichier précédent (", str(e)[:100], ")")
        return {}


def main(dst):
    prev = precedent()
    spot = {}
    with Navigateur() as nav:
        for cle, url in (("or", URL_OR), ("ag", URL_ARGENT)):
            try:
                _, texte = nav.page(url, attendre_texte=r"par\s+kilo", bonus=r"Semaine\s+Variation")
                kg, heure = parse_bullionvault(texte)
                if kg is None:
                    raise ValueError("prix au kilo introuvable")
                spot[cle] = {"kg": kg, "heure": heure, "sem": parse_variation_semaine(texte)}
                print(cle, kg, spot[cle]["sem"])
            except Exception as e:  # noqa: BLE001
                print("ÉCHEC", cle, str(e)[:150])
    if len(spot) < 2:
        # un cours manque : on le reprend du relevé précédent plutôt que d'afficher un trou
        for cle, k in (("or", "or_kg"), ("ag", "ag_kg")):
            if cle not in spot and prev.get(k):
                spot[cle] = {"kg": prev[k], "heure": prev.get("heure_" + cle), "sem": prev.get("var_" + cle)}
        if len(spot) < 2:
            sys.exit("Cours introuvables et pas de relevé précédent : on ne publie pas.")

    try:
        from zoneinfo import ZoneInfo
        aujourdhui = dt.datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    except Exception:  # noqa: BLE001
        aujourdhui = dt.date.today().isoformat()
    hist = {k: v for k, v in (prev.get("historique") or {}).items() if isinstance(v, dict)}
    hist[aujourdhui] = {"or_kg": spot["or"]["kg"], "ag_kg": spot["ag"]["kg"]}
    for k in sorted(hist)[:-12]:
        del hist[k]
    veille = [k for k in sorted(hist) if k < aujourdhui]

    def jour(cle):
        if not veille or not hist[veille[-1]].get(cle):
            return None
        ref, act = hist[veille[-1]][cle], hist[aujourdhui][cle]
        return {"eur": round(act - ref, 2), "pct": round((act / ref - 1) * 100, 2), "depuis": veille[-1]}

    out = {
        "genere_le": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "or_kg": spot["or"]["kg"], "ag_kg": spot["ag"]["kg"],
        "var_or": spot["or"]["sem"], "var_ag": spot["ag"]["sem"],
        "jour_or": jour("or_kg"), "jour_ag": jour("ag_kg"),
        "heure_or": spot["or"]["heure"], "heure_ag": spot["ag"]["heure"],
        "historique": hist,
    }
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("écrit", dst)


if __name__ == "__main__":
    main(sys.argv[1])
