"""Interface web de validation (FastAPI + gabarits Jinja2 + HTMX), protégée par mot de passe.

Lancement : `agence web`. Rien n'est publié automatiquement d'ici : l'interface sert à valider ou refuser,
puis à télécharger un contenu validé et à noter sa publication manuelle.
"""

import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agents.publication import dossier_public
from app import equipe, explications, graphiques, recherche, securite
from app.auth import DUREE_SESSION, creer_session, nouveau_secret, session_valide, verifier
from noyau.base import connecter
from noyau.config import RACINE, Config, ecrire_env
from noyau.couts import JournalCouts
from noyau.erreurs import ErreurDefinitive, ErreurTemporaire
from noyau.instagram import ClientInstagram
from noyau.creneaux import Creneaux
from noyau.direction import DirectionErreur, Directions
from noyau.calendrier import Calendrier, ElementPublication, lundi_de
from noyau.file import File, TacheIntrouvable, TransitionInterdite
from noyau.horloge import Horloge, maintenant_utc
from noyau.lecons import LeconErreur, Lecons
from noyau.modifications import ModificationErreur, Modifications
from noyau.notifications import Notifications
from noyau.messages import DemandeAnalyse, MessageInvalide, valider
from noyau.planificateur import Planificateur, lundi_suivant
from noyau.publication import PublicationErreur, Publications
from noyau.statistiques import CHIFFRES, LIBELLES, Statistiques, StatistiquesErreur, engagement, explication
from noyau.purge import Purge
from noyau.claude import MoteurClaude
from noyau.pause import DUREES_HEURES, Pause
from noyau.quota import Quota
from noyau.references import References
from noyau.rejets import rejeter
from noyau.banque_sons import CATEGORIES as CATEGORIES_SONS, Banque, BanqueErreur
from noyau.stockage import Bibliotheque, StockageErreur, dossier_de_travail
from noyau import bilan, sauvegarde

FUSEAU = ZoneInfo("Europe/Paris")
DOSSIER = Path(__file__).resolve().parent
COOKIE = "agence_session"
RESEAUX = ("instagram", "linkedin", "facebook", "tiktok", "x")
journal = logging.getLogger("agence.web")
mimetypes.add_type("application/manifest+json", ".webmanifest")
CHEMINS_PUBLICS = ("/connexion", "/statique/", "/public/", "/manifest.webmanifest")
# Agents à qui s'adresse une règle ajoutée par l'humain (page Consignes), et ceux d'une règle tirée d'une
# demande de modification du texte seulement.
AGENTS_REGLE = ("orchestrateur_marketing", "agent_redacteur", "agent_visuel", "agent_video", "agent_relecteur")
AGENTS_REGLE_TEXTE = ["agent_redacteur", "agent_relecteur"]
# Pages rangées dans « Réglages » (consultées de temps en temps) : le menu y reste sur « Réglages ».
PAGES_REGLAGES = ("/direction", "/references", "/rushs", "/sons", "/corbeille", "/historique", "/echecs")


def rubrique(chemin: str) -> str:
    premier = "/" + chemin.split("/")[1]
    return "/reglages" if premier in PAGES_REGLAGES else premier
# Images exposées le temps que Meta les récupère : nom aléatoire (agents/publication.py), durée limitée.
NOM_PUBLIC = re.compile(r"[A-Za-z0-9_-]{32}\.(jpg|mp4)")
DUREE_PUBLIQUE = timedelta(hours=1)


@dataclass
class Services:
    conn: sqlite3.Connection
    file: File
    biblio: Bibliotheque
    couts: JournalCouts
    purge: Purge
    lecons: Lecons
    references: References
    directions: Directions


def date_fr(valeur: str | datetime | None) -> str:
    if not valeur:
        return "-"
    date = datetime.fromisoformat(valeur) if isinstance(valeur, str) else valeur
    return date.astimezone(FUSEAU).strftime("%d/%m/%Y %H:%M")


def depuis(date: datetime, maintenant: datetime) -> str:
    minutes = int((maintenant - date).total_seconds() // 60)
    if minutes < 1:
        return "à l'instant"
    if minutes < 60:
        return f"il y a {minutes} min"
    if minutes < 48 * 60:
        return f"il y a {minutes // 60} h"
    return f"le {date_fr(date)}"


# Lisibilité des rapports (affichage seulement : les agents gardent le texte complet).
FIN_DE_PHRASE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Ý«\"(0-9])")
COUPURE = re.compile(r"\.\s|\s:\s|\s;\s|\s—\s|\s–\s")
MOTS_TITRE_MAX = 14
MOTS_TITRE_MIN = 4


def phrases(texte: str | None) -> list[str]:
    return [p.strip() for p in FIN_DE_PHRASE.split(texte or "") if p.strip()]


def point(texte: str) -> dict:
    """Un élément de liste en titre court (première proposition) et détail complet, replié."""
    texte = (texte or "").strip()
    fin = len(texte)
    for coupure in COUPURE.finditer(texte):
        # Un début trop court (« Semaine du 02/11 : … ») est complété jusqu'à la proposition suivante.
        if coupure.group().startswith(".") or len(texte[:coupure.start()].split()) >= MOTS_TITRE_MIN:
            fin = coupure.start()
            break
    titre = texte[:fin].strip()
    mots = titre.split()
    if len(mots) > MOTS_TITRE_MAX:
        titre = " ".join(mots[:MOTS_TITRE_MAX - 2]) + "…"
    titre = titre.rstrip(" .:;")
    return {"titre": titre, "detail": texte if titre.rstrip("…") != texte.rstrip(" .") else ""}


def rediriger(chemin: str, **message: str) -> RedirectResponse:
    return RedirectResponse(f"{chemin}?{urlencode(message)}" if message else chemin, status_code=303)


def creer_app(config: Config, horloge: Horloge = maintenant_utc, racine: Path = RACINE) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/statique", StaticFiles(directory=DOSSIER / "statique"), name="statique")
    gabarits = Jinja2Templates(directory=DOSSIER / "gabarits")
    gabarits.env.filters["date_fr"] = date_fr
    gabarits.env.filters["lien_sur"] = securite.lien_sur
    gabarits.env.filters["expliquer"] = explications.expliquer
    limite = securite.LimiteConnexions()
    # Secret des cookies de session : remplacé par « Déconnecter tous les appareils » (aussi écrit dans .env).
    session = {"secret": config.secret_session}
    gabarits.env.filters["phrases"] = phrases
    gabarits.env.filters["en_date"] = datetime.fromisoformat
    gabarits.env.filters["point"] = point
    gabarits.env.filters["jour_fr"] = lambda valeur: date.fromisoformat(str(valeur)).strftime("%d/%m/%Y")
    gabarits.env.filters["champ_date"] = lambda valeur: datetime.fromisoformat(valeur).astimezone(FUSEAU).strftime("%Y-%m-%dT%H:%M")
    gabarits.env.filters["depuis"] = lambda date: depuis(date, horloge())
    # Tâches qui attendent toutes la nuit (rien ne tourne encore) : « prévu cette nuit », pas « en cours ».
    gabarits.env.filters["de_nuit"] = lambda taches: bool(taches) and all(
        t.statut == "à_faire" and t.creneau != "jour" for t in taches)
    # Change à chaque modification du style : le navigateur ne garde pas une ancienne version en cache.
    gabarits.env.globals["nom_agence"] = config.nom_agence
    gabarits.env.globals["initiale"] = (config.nom_agence.strip()[:1] or "A").upper()
    gabarits.env.globals["version_statique"] = int(max(f.stat().st_mtime for f in (DOSSIER / "statique").glob("*.*")))

    @contextmanager
    def ouvrir() -> Iterator[Services]:
        # Une connexion par requête, ouverte dans le fil qui la traite (SQLite l'exige).
        conn = connecter(config.chemin_base)
        try:
            biblio = Bibliotheque.depuis_config(conn, config, horloge)
            file = File(conn, horloge)
            yield Services(conn, file, biblio, JournalCouts.depuis_config(conn, config, horloge),
                           Purge.depuis_config(biblio, config), Lecons(conn, file, racine, horloge),
                           References(conn, biblio.stockage, config.dossier_temp, horloge),
                           Directions(conn, racine, horloge))
        finally:
            conn.close()

    def statistiques(s: Services) -> Statistiques:
        return Statistiques(s.file, Publications(s.file, s.biblio, horloge), horloge)

    def page(request: Request, gabarit: str, s: Services, statut: int = 200, **contexte) -> Response:
        a_valider = [t for t in s.file.lister(statut="à_valider") if t.type != "lecon"]
        bandeau = {
            "quota": Quota(s.conn, horloge).actuel(),
            "quota_mesure": Quota(s.conn, horloge).derniere_mesure(),
            "quota_seuil": config.quota_seuil_pourcent,
            "disque": s.purge.etat_disque(),
            "nb_a_valider": len(a_valider),
            "nb_a_publier": len(Publications(s.file, s.biblio, horloge).a_publier()),
            "nb_echecs": len(s.file.lister(statut="échec")),
            "nb_corbeille": len(s.biblio.lister("corbeille")),
            "nb_lecons": len(s.lecons.lister("proposée")),
            "nb_directions": len(s.directions.lister("proposée")),
            "pause": Pause(s.conn, horloge).etat(),
            "durees_pause": DUREES_HEURES,
        }
        return gabarits.TemplateResponse(request, gabarit, {
            "bandeau": bandeau, "actif": rubrique(request.url.path), "info": request.query_params.get("info"),
            "erreur": request.query_params.get("erreur"), **contexte,
        }, status_code=statut)

    @app.middleware("http")
    async def authentification(request: Request, suite):
        if request.url.path.startswith(CHEMINS_PUBLICS) or session_valide(request.cookies.get(COOKIE), session["secret"]):
            reponse = await suite(request)
        else:
            reponse = RedirectResponse("/connexion", status_code=303)
        for nom, valeur in securite.ENTETES.items():
            reponse.headers.setdefault(nom, valeur)
        return reponse

    def poser_session(request: Request, reponse: Response) -> Response:
        # Derrière Caddy, la requête arrive en http : l'adresse publique dit si le site est servi en HTTPS.
        reponse.set_cookie(COOKIE, creer_session(session["secret"]), max_age=DUREE_SESSION, httponly=True,
                           samesite="strict",
                           secure=request.url.scheme == "https" or config.url_publique.startswith("https://"))
        return reponse

    @app.get("/public/{nom}")
    def image_publique(nom: str):
        """Seule page sans mot de passe : une image ou une vidéo en cours de publication, à l'adresse imprévisible."""
        fichier = dossier_public(config) / nom
        if not NOM_PUBLIC.fullmatch(nom) or not fichier.is_file() or \
                time.time() - fichier.stat().st_mtime > DUREE_PUBLIQUE.total_seconds():
            return Response("Introuvable", status_code=404)
        return FileResponse(fichier, media_type="video/mp4" if fichier.suffix == ".mp4" else "image/jpeg")

    # --- Connexion ---

    @app.get("/manifest.webmanifest")
    def manifeste():
        """Manifeste de l'application installable, au nom de l'agence (NOM_AGENCE)."""
        icone = lambda taille: {"src": f"/statique/icones/icone-{taille}.png", "sizes": f"{taille}x{taille}"}
        return JSONResponse({
            "name": config.nom_agence, "short_name": config.nom_agence[:12],
            "description": "Validez les posts de l'agence et suivez son travail.",
            "lang": "fr", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#fafaf9", "theme_color": "#fafaf9",
            "icons": [{**icone(t), "type": "image/png", "purpose": "any maskable"} for t in (192, 512)],
            "shortcuts": [{"name": "À valider", "url": "/a-valider", "icons": [icone(192)]},
                          {"name": "Agents", "url": "/agents", "icons": [icone(192)]}],
        }, media_type="application/manifest+json")

    @app.get("/connexion")
    def connexion(request: Request):
        return gabarits.TemplateResponse(request, "connexion.html", {"erreur": None})

    @app.post("/connexion")
    def se_connecter(request: Request, mot_de_passe: str = Form("")):
        adresse = request.client.host if request.client else "?"
        if attente := limite.attente(adresse, time.time()):
            return gabarits.TemplateResponse(request, "connexion.html", {
                "erreur": f"Trop d'essais. Réessayez dans {max(1, attente // 60)} min."}, status_code=429)
        if not config.mot_de_passe_interface or not verifier(mot_de_passe, config.mot_de_passe_interface):
            time.sleep(1)  # freine les essais de mots de passe
            if limite.echec(adresse, time.time()):
                journal.warning("Connexion bloquée 15 min après des essais de mot de passe répétés (%s)", adresse)
                with ouvrir() as s:
                    notifications = Notifications(config, s.conn, horloge)
                    if notifications.actives:
                        notifications.envoyer("Essais de connexion bloqués",
                                              f"Mots de passe erronés répétés depuis {adresse} : connexion bloquée 15 min.",
                                              priorite=4, tags=("warning",), chemin="/reglages")
            return gabarits.TemplateResponse(request, "connexion.html", {"erreur": "Mot de passe incorrect."},
                                             status_code=401)
        limite.reussite(adresse)
        return poser_session(request, RedirectResponse("/", status_code=303))

    @app.post("/deconnecter-partout")
    def deconnecter_partout(request: Request):
        """Nouveau secret de session : tous les cookies déjà donnés (téléphone perdu, ordinateur prêté) deviennent
        invalides ; cet appareil reçoit un nouveau cookie."""
        session["secret"] = nouveau_secret()
        ecrire_env({"SECRET_SESSION": session["secret"]}, racine / ".env")
        return poser_session(request, rediriger("/reglages", info="Tous les autres appareils sont déconnectés."))

    @app.post("/deconnexion")
    def se_deconnecter():
        reponse = RedirectResponse("/connexion", status_code=303)
        reponse.delete_cookie(COOKIE)
        return reponse

    # --- File de validation ---

    # --- Accueil : tableau de bord ---

    @app.get("/")
    def accueil(request: Request):
        with ouvrir() as s:
            maintenant = horloge().astimezone(FUSEAU)
            # Statistiques : les 12 derniers posts, du plus ancien au plus récent.
            stats = statistiques(s)
            lignes = list(reversed(stats.lister(12)))
            posts = [{"date": datetime.fromisoformat(l["publie_le"]).astimezone(FUSEAU).strftime("%d/%m"),
                      "type": l["type"], "vues": l["vues"], "couverture": l["couverture"], "engagement": engagement(l)}
                     for l in lignes if l["publie_le"]]

            def moyenne(cle):
                valeurs = [p[cle] for p in posts if p[cle] is not None]
                return sum(valeurs) / len(valeurs) if valeurs else None

            formats = []
            for nom in graphiques.COULEURS_FORMATS:
                valeurs = [p["couverture"] for p in posts if p["type"] == nom and p["couverture"] is not None]
                if valeurs:
                    formats.append({"format": nom, "valeur": sum(valeurs) / len(valeurs), "posts": len(valeurs)})
            indicateurs = {"posts": len(posts), "vues": moyenne("vues"), "couverture": moyenne("couverture"),
                           "engagement": moyenne("engagement"),
                           "periode": (posts[0]["date"], posts[-1]["date"]) if posts else None}

            # À faire : contenus et leçons à valider, travail des agents.
            a_valider = [{"tache": t, "medias": medias_de(s, t)} for t in s.file.lister(statut="à_valider")
                         if t.type != "lecon"]
            lecons = s.lecons.lister("proposée")
            en_cours = sorted((t for t in s.file.lister() if t.statut in ("en_cours", "à_faire")),
                              key=lambda t: (t.statut != "en_cours", t.id))
            calendrier = Calendrier(s.file, Publications(s.file, s.biblio, horloge))
            prochaines = [e for jour in calendrier.semaine(lundi_de(maintenant.date()), maintenant)
                          + calendrier.semaine(lundi_de(maintenant.date()) + timedelta(days=7), maintenant)
                          for e in jour.elements if isinstance(e, ElementPublication) and e.quand >= maintenant
                          and e.etat != "Publié"][:4]
            vignettes = {e.racine.id: (medias_de(s, e.post)[:1] if e.post else []) for e in prochaines}
            # Post star : le plus vu du mois, avec ce qu'en dit le dernier rapport de l'analyste.
            star = None
            if trouve := stats.post_star():
                ligne, du_mois = trouve
                rapport = s.conn.execute("SELECT contenu FROM rapports_analyste ORDER BY id DESC LIMIT 1").fetchone()
                publie_le = datetime.fromisoformat(ligne["publie_le"])
                post = s.file.lire(ligne["post_id"]) if ligne["post_id"] else None
                star = {"ligne": ligne, "du_mois": du_mois, "publie_le": publie_le, "engagement": engagement(ligne),
                        "medias": medias_de(s, post)[:1] if post else [],
                        "explication": explication(json.loads(rapport["contenu"]) if rapport else None, publie_le)}

            # Santé du système : sauvegarde, disque, relevé Instagram, notifications.
            sauvegardes = sauvegarde.lister(config.dossier_sauvegardes)
            derniere_sauvegarde = datetime.fromtimestamp(sauvegardes[-1].stat().st_mtime, UTC) if sauvegardes else None
            dernier_releve = stats.dernier_releve_api()
            sante = {"sauvegarde": derniere_sauvegarde,
                     "sauvegarde_ok": bool(derniere_sauvegarde and maintenant - derniere_sauvegarde < timedelta(hours=36)),
                     "instagram": bool(config.instagram_jeton and config.instagram_compte_id),
                     "releve": dernier_releve,
                     "releve_ok": bool(dernier_releve and maintenant - dernier_releve < timedelta(hours=24)),
                     "notifications": bool(config.ntfy_sujet)}
            quota = Quota(s.conn, horloge)
            attente = quota.attente_avant_appel(config.quota_seuil_pourcent)
            return page(request, "accueil.html", s, maintenant=maintenant, posts=posts, indicateurs=indicateurs,
                        g_vues=graphiques.colonnes_par_post(posts, "vues", "vues"),
                        g_engagement=graphiques.ligne_par_post(posts, "engagement", indicateurs["engagement"]),
                        g_formats=graphiques.barres_par_format(formats),
                        legende=graphiques.legende_formats({p["type"] for p in posts}),
                        a_valider=a_valider, lecons=lecons, en_cours=en_cours[:6], nb_en_cours=len(en_cours),
                        prochaines=prochaines, vignettes=vignettes, en_pause=attente is not None,
                        nuit=Creneaux.depuis_config(config, s.conn, horloge).periode_nuit(),
                        abonnes=stats.abonnes(), courbe=graphiques.courbe_abonnes(stats.courbe_abonnes()),
                        star=star, sante=sante, nombre=graphiques.nombre, un_jour=timedelta(days=1),
                        # Lundi et mardi : bilan de la semaine écoulée en tête de l'accueil.
                        bilan=bilan.bilan(s.conn, bilan.semaine_ecoulee(maintenant)) if maintenant.weekday() < 2 else None)

    @app.get("/agents")
    @app.get("/agents/{agent_id}")
    def agents(request: Request, agent_id: str | None = None, tache: int | None = None):
        if agent_id is not None and agent_id not in equipe.PAR_ID:
            return rediriger("/agents", erreur="Agent inconnu.")
        with ouvrir() as s:
            maintenant = horloge()
            return page(request, "agents.html", s, etats=equipe.etats(s.file, maintenant), maintenant=maintenant,
                        choisi=agent_id, ouverte=tache, historique=equipe.historique(s.file, agent_id) if agent_id else [],
                        chaines=equipe.chaines(s.file), AGENTS=equipe.AGENTS, NIVEAUX=equipe.NIVEAUX,
                        duree=equipe.duree, centres=equipe.centres, nombre=graphiques.nombre)

    @app.get("/a-valider")
    def a_valider(request: Request):
        with ouvrir() as s:
            posts = []
            for tache in s.file.lister(statut="à_valider"):
                if tache.type == "lecon":
                    continue
                posts.append({"tache": tache, "medias": medias_de(s, tache), "visuel": origine_visuel(s, tache)})
            return page(request, "a_valider.html", s, posts=posts)

    @app.get("/grille")
    def grille(request: Request, post: int | None = None):
        """Aperçu du profil Instagram : les posts prévus (à valider ou validés) devant les derniers posts publiés,
        pour juger avant de valider si un visuel s'accorde avec le reste de la grille."""
        with ouvrir() as s:
            publies_agence = {l["tache_id"]: l for l in s.conn.execute("SELECT tache_id, publie_le, lien FROM publications")}
            prevus = []
            for tache in s.file.lister():
                if tache.type == "post_brouillon" and tache.statut in ("à_valider", "validé") and tache.id not in publies_agence:
                    prevus.append({"tache": tache, "medias": medias_de(s, tache),
                                   "quand": datetime.fromisoformat(tache.contenu["horaire_prevu"])})
            prevus.sort(key=lambda p: p["quand"], reverse=True)  # le plus tardif en haut à gauche, comme sur Instagram
            stats = statistiques(s)
            lignes = stats.lister(24)
            vus = {l["post_id"] for l in lignes if l["post_id"]}
            publies = [{"quand": datetime.fromisoformat(l["publie_le"]), "type": l["type"], "lien": l["lien"],
                        "apercu": l["apercu"], "vues": l["vues"],
                        "medias": medias_de(s, s.file.lire(l["post_id"]))[:1] if l["post_id"] else []} for l in lignes]
            # Posts de l'agence publiés mais pas encore relevés (le relevé passe toutes les 12 h). Au-delà de 48 h, un post
            # que le relevé n'a pas retrouvé n'est pas (ou plus) sur Instagram : il n'a pas sa place dans la grille.
            recents = horloge() - timedelta(hours=48)
            for tache_id, ligne in publies_agence.items():
                if tache_id not in vus and datetime.fromisoformat(ligne["publie_le"]) >= recents:
                    tache = s.file.lire(tache_id)
                    medias = medias_de(s, tache)
                    publies.append({"quand": datetime.fromisoformat(ligne["publie_le"]), "lien": ligne["lien"],
                                    "type": "reel" if medias and medias[0].type == "vidéo" else
                                            ("carrousel" if len(medias) > 1 else "image"),
                                    "apercu": None, "vues": None, "medias": medias[:1]})
            publies.sort(key=lambda p: p["quand"], reverse=True)
            nb_publies = s.conn.execute("SELECT count(*) FROM statistiques").fetchone()[0]
            return page(request, "grille.html", s, prevus=prevus, publies=publies[:max(0, 18 - len(prevus))],
                        choisi=post, abonnes=stats.abonnes(), nb_publies=nb_publies, nombre=graphiques.nombre,
                        fuseau=FUSEAU)

    @app.get("/a-valider/nouveautes")
    def nouveautes(vus: str = ""):
        """Fragment pour la page À valider : bandeau s'il est arrivé des contenus depuis l'affichage, sinon rien."""
        deja = {int(i) for i in vus.split(",") if i.isdigit()}
        with ouvrir() as s:
            nouveaux = [t for t in s.file.lister(statut="à_valider") if t.type != "lecon" and t.id not in deja]
        if not nouveaux:
            return Response("")
        n = len(nouveaux)
        return Response(f'<a href="/a-valider#post-{nouveaux[0].id}" class="bandeau-nouveautes">'
                        f'<span class="point-etat actif"></span>{n} nouveau{"x" if n > 1 else ""} post{"s" if n > 1 else ""} '
                        f'à valider<strong>Afficher</strong></a>', media_type="text/html")

    def origine_visuel(s: Services, tache) -> dict | None:
        """Pour un post issu d'un visuel créé : prompt utilisé et coût des appels payants de la chaîne."""
        if not tache.parent_id:
            return None
        parent = s.file.lire(tache.parent_id)
        if parent.type != "asset":
            return None
        cout = s.conn.execute("SELECT COALESCE(SUM(cout_usd), 0) FROM couts WHERE tache_id IN (?, ?)",
                              (parent.contenu["brief_id"], parent.id)).fetchone()[0]
        return {"prompt": parent.contenu["prompt_ou_script"], "cout_usd": cout,
                "reel": parent.contenu["type_media"] == "vidéo"}

    @app.post("/taches/{tache_id}/valider")
    def valider_tache(tache_id: int, horaire: str = Form(""), etiquette_ia: str = Form("")):
        with ouvrir() as s:
            try:
                if s.file.lire(tache_id).type == "post_brouillon":
                    champs = {"etiquette_ia": bool(etiquette_ia)}
                    if horaire:
                        champs["horaire_prevu"] = datetime.fromisoformat(horaire).replace(tzinfo=FUSEAU)
                    s.file.modifier(tache_id, **champs)
                s.file.valider(tache_id)
            except (TransitionInterdite, MessageInvalide, ValueError) as erreur:
                return rediriger("/a-valider", erreur=str(erreur))
        return rediriger("/a-valider", info=f"Contenu {tache_id} validé.")

    @app.post("/taches/{tache_id}/refuser")
    def refuser_tache(tache_id: int, commentaire: str = Form("")):
        with ouvrir() as s:
            try:
                rejeter(s.file, s.biblio, tache_id, "refus_humain", commentaire)
            except TransitionInterdite as erreur:
                return rediriger("/a-valider", erreur=str(erreur))
        return rediriger("/a-valider", info=f"Contenu {tache_id} refusé. Il sera analysé pour en tirer une leçon.")

    @app.post("/taches/{tache_id}/ecarter")
    def ecarter_tache(tache_id: int, commentaire: str = Form("")):
        with ouvrir() as s:
            try:
                rejeter(s.file, s.biblio, tache_id, "ne_pas_utiliser", commentaire)
            except TransitionInterdite as erreur:
                return rediriger("/a-valider", erreur=str(erreur))
        return rediriger("/a-valider", info=f"Contenu {tache_id} écarté. Il sera analysé pour en tirer une leçon.")

    # --- Nouveau brief ---

    @app.get("/brief")
    def formulaire_brief(request: Request):
        with ouvrir() as s:
            return page(request, "brief.html", s, reseaux=RESEAUX, rushs=s.biblio.lister("actif", origine="rush"),
                        valeurs={}, erreurs=None)

    @app.post("/brief")
    def creer_brief(request: Request, objectif: str = Form(""), format: str = Form(""), reseau: str = Form(""),
                    date_prevue: str = Form(""), rushs: list[int] = Form([]), type_brief: str = Form("visuel"),
                    tout_de_suite: str = Form("")):
        valeurs = {"objectif": objectif, "format": format, "reseau": reseau, "date_prevue": date_prevue,
                   "rushs": rushs, "type_brief": type_brief, "tout_de_suite": bool(tout_de_suite)}
        with ouvrir() as s:
            try:
                date = datetime.fromisoformat(date_prevue).replace(tzinfo=FUSEAU) if date_prevue else None
                type_message = {"visuel": "brief_visuel", "video": "brief_video"}.get(type_brief, "brief_texte")
                message = valider(type_message, {"objectif": objectif, "format": format, "reseau": reseau,
                                                  "date_prevue": date, "rushs": rushs})
            except (MessageInvalide, ValueError) as erreur:
                return page(request, "brief.html", s, statut=400, reseaux=RESEAUX,
                            rushs=s.biblio.lister("actif", origine="rush"), valeurs=valeurs, erreurs=str(erreur))
            creneau = "jour" if tout_de_suite else \
                Creneaux.depuis_config(config, s.conn, horloge).decider(message.date_prevue)
            tache_id = s.file.creer(message, creneau=creneau)
        if creneau == "nuit":
            return rediriger("/calendrier", info=f"Brief {tache_id} enregistré : produit cette nuit, prêt à valider demain matin.")
        return rediriger("/a-valider", info=f"Brief {tache_id} enregistré : le post arrivera ici une fois rédigé.")

    # --- Rushs ---

    @app.get("/rushs")
    def rushs(request: Request):
        with ouvrir() as s:
            return page(request, "rushs.html", s, rushs=s.biblio.lister("actif", origine="rush"))

    @app.post("/rushs")
    def ajouter_rushs(fichiers: list[UploadFile]):
        ajoutes, refus = 0, []
        with ouvrir() as s:
            for fichier in fichiers:
                if not fichier.filename:
                    continue
                with dossier_de_travail(config.dossier_temp) as dossier:
                    cible = dossier / Path(fichier.filename).name
                    with cible.open("wb") as sortie:
                        shutil.copyfileobj(fichier.file, sortie)
                    try:
                        s.biblio.ajouter(cible, "rush")
                        ajoutes += 1
                    except StockageErreur as erreur:
                        refus.append(f"{fichier.filename} : {erreur}")
        message = {"info": f"{ajoutes} rush(s) ajouté(s)."}
        if refus:
            message["erreur"] = " ; ".join(refus)
        return rediriger("/rushs", **message)

    @app.post("/rushs/{asset_id}/ecarter")
    def ecarter_rush(asset_id: int):
        with ouvrir() as s:
            try:
                if s.biblio.lire(asset_id).origine != "rush":
                    return rediriger("/rushs", erreur=f"L'élément {asset_id} n'est pas un rush.")
                s.biblio.mettre_en_corbeille(asset_id, "ne_pas_utiliser")
            except StockageErreur as erreur:
                return rediriger("/rushs", erreur=str(erreur))
        return rediriger("/rushs", info=f"Rush {asset_id} mis en corbeille (restaurable pendant "
                                        f"{config.corbeille_delai_heures:g} h).")

    # --- Banque de bruitages (écoute et tri par l'humain) ---

    def banque(s: Services) -> Banque:
        return Banque(s.conn, s.biblio.stockage, config.dossier_temp, horloge)

    @app.get("/sons")
    def page_sons(request: Request):
        with ouvrir() as s:
            sons = banque(s).lister(None)
            delai = timedelta(hours=config.corbeille_delai_heures)
            categories = {}
            for son in sons:
                if son.etat == "actif":
                    categories.setdefault(son.categorie, []).append(son)
            ecartes = [{"son": son, "suppression": datetime.fromisoformat(son.corbeille_le) + delai}
                       for son in sons if son.etat == "corbeille"]
            return page(request, "sons.html", s, categories=categories, ecartes=ecartes,
                        usages={nom: usage for nom, (_, usage) in CATEGORIES_SONS.items()})

    @app.get("/sons/{son_id}/audio")
    def audio_son(son_id: int):
        with ouvrir() as s:
            try:
                son = banque(s).lire(son_id)
                if not son.chemin:
                    raise BanqueErreur("supprimé")
                return FileResponse(s.biblio.stockage.chemin_local(son.chemin), media_type="audio/wav")
            except (BanqueErreur, StockageErreur):
                return Response("Son introuvable", status_code=404)

    @app.post("/sons/{son_id}/ecarter")
    def ecarter_son(son_id: int):
        with ouvrir() as s:
            try:
                banque(s).ecarter(son_id)
            except BanqueErreur as erreur:
                return rediriger("/sons", erreur=str(erreur))
        return rediriger("/sons", info=f"Son {son_id} écarté (restaurable pendant {config.corbeille_delai_heures:g} h).")

    @app.post("/sons/{son_id}/restaurer")
    def restaurer_son(son_id: int):
        with ouvrir() as s:
            try:
                banque(s).restaurer(son_id)
            except BanqueErreur as erreur:
                return rediriger("/sons", erreur=str(erreur))
        return rediriger("/sons", info=f"Son {son_id} restauré.")

    @app.get("/media/{asset_id}")
    def media(asset_id: int):
        with ouvrir() as s:
            try:
                return FileResponse(s.biblio.chemin_local(asset_id))
            except StockageErreur:
                return Response("Média introuvable", status_code=404)

    @app.get("/media/{asset_id}/vignette")
    def media_vignette(asset_id: int):
        with ouvrir() as s:
            try:
                return FileResponse(s.biblio.chemin_vignette(asset_id))
            except StockageErreur:
                return Response("Média introuvable", status_code=404)

    # --- Références de style ---

    @app.get("/references")
    def references(request: Request):
        with ouvrir() as s:
            return page(request, "references.html", s, references=s.references.lister())

    @app.post("/references")
    def ajouter_references(fichiers: list[UploadFile]):
        ajoutees, refus = 0, []
        with ouvrir() as s:
            for fichier in fichiers:
                if not fichier.filename:
                    continue
                with dossier_de_travail(config.dossier_temp) as dossier:
                    cible = dossier / Path(fichier.filename).name
                    with cible.open("wb") as sortie:
                        shutil.copyfileobj(fichier.file, sortie)
                    try:
                        s.references.ajouter(cible)
                        ajoutees += 1
                    except StockageErreur as erreur:
                        refus.append(f"{fichier.filename} : {erreur}")
        message = {"info": f"{ajoutees} référence(s) ajoutée(s)."}
        if refus:
            message["erreur"] = " ; ".join(refus)
        return rediriger("/references", **message)

    @app.post("/references/analyser")
    def analyser_references():
        with ouvrir() as s:
            identifiants = [r.id for r in s.references.lister()]
            if not identifiants:
                return rediriger("/references", erreur="Ajoutez d'abord des références à analyser.")
            s.file.creer(valider("analyse_style", {"references": identifiants}))
        return rediriger("/direction", info=f"Analyse de {len(identifiants)} référence(s) lancée : la direction "
                                            "artistique proposée apparaîtra ici.")

    @app.get("/direction")
    def direction(request: Request):
        with ouvrir() as s:
            en_cours = [t for t in s.file.lister(agent="agent_visuel") if t.type == "analyse_style"
                        and t.statut in ("à_faire", "en_cours")]
            return page(request, "direction.html", s, active=s.directions.active(),
                        proposees=s.directions.lister("proposée"), analyse_en_cours=bool(en_cours))

    @app.post("/direction/{direction_id}/valider")
    def valider_direction(direction_id: int, texte: str = Form("")):
        with ouvrir() as s:
            try:
                s.directions.valider(direction_id, texte)
            except DirectionErreur as erreur:
                return rediriger("/direction", erreur=str(erreur))
        return rediriger("/direction", info="Direction artistique validée : les prochains visuels la suivront.")

    @app.post("/direction/{direction_id}/refuser")
    def refuser_direction(direction_id: int, commentaire: str = Form("")):
        with ouvrir() as s:
            try:
                s.directions.refuser(direction_id, commentaire)
            except DirectionErreur as erreur:
                return rediriger("/direction", erreur=str(erreur))
        return rediriger("/direction", info="Proposition refusée. Ajustez vos références puis relancez l'analyse.")

    @app.get("/references/{reference_id}/image")
    def image_reference(reference_id: int):
        with ouvrir() as s:
            try:
                return FileResponse(s.references.chemin_local(reference_id))
            except StockageErreur:
                return Response("Référence introuvable", status_code=404)

    @app.post("/references/{reference_id}/retirer")
    def retirer_reference(reference_id: int):
        with ouvrir() as s:
            try:
                s.references.retirer(reference_id)
            except StockageErreur as erreur:
                return rediriger("/references", erreur=str(erreur))
        return rediriger("/references", info=f"Référence {reference_id} retirée.")

    # --- Publication manuelle (boutons du calendrier) ---

    def medias_de(s: Services, tache) -> list:
        medias = []
        for asset_id in tache.contenu.get("medias", []):
            try:
                medias.append(s.biblio.lire(asset_id))
            except StockageErreur:
                pass
        return medias

    @app.get("/taches/{tache_id}/export")
    def exporter(tache_id: int):
        with ouvrir() as s:
            try:
                archive = Publications(s.file, s.biblio, horloge).exporter(tache_id, config.dossier_temp)
            except (PublicationErreur, StockageErreur) as erreur:
                return rediriger("/calendrier", erreur=str(erreur))

        def contenu():
            with archive:
                while bloc := archive.read(1024 * 1024):
                    yield bloc

        return StreamingResponse(contenu(), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="post-{tache_id}.zip"'})

    @app.post("/taches/{tache_id}/publie")
    def marquer_publie(tache_id: int, retour: str = Form("/calendrier")):
        retour = retour if retour.startswith("/calendrier") else "/calendrier"
        with ouvrir() as s:
            try:
                Publications(s.file, s.biblio, horloge).marquer_publie(tache_id)
            except (PublicationErreur, StockageErreur) as erreur:
                return rediriger(retour, erreur=str(erreur))
        return rediriger(retour, info=f"Contenu {tache_id} marqué comme publié. Ses médias produits seront "
                                      f"supprimés dans {config.publication_delai_jours} jours.")

    # --- Historique ---

    @app.get("/recherche")
    def rechercher(request: Request, q: str = ""):
        """Posts de l'agence (toutes versions) et posts Instagram relevés dont la légende contient tous les mots."""
        mots = recherche.termes(q)
        resultats = []
        with ouvrir() as s:
            if mots:
                publications = {l["tache_id"]: l for l in s.conn.execute("SELECT tache_id, publie_le, lien FROM publications")}
                for tache in s.file.lister():
                    if tache.type == "post_brouillon" and recherche.correspond(tache.contenu.get("texte"), mots):
                        resultats.append({"tache": tache, "quand": tache.contenu.get("horaire_prevu") or tache.cree_le,
                                          "medias": medias_de(s, tache)[:1], "publication": publications.get(tache.id),
                                          "extrait": recherche.extrait(tache.contenu["texte"], mots)})
                # Posts Instagram qui ne viennent pas de l'agence (ceux de l'agence sont déjà trouvés ci-dessus).
                for ligne in s.conn.execute("SELECT * FROM statistiques WHERE post_id IS NULL"):
                    if recherche.correspond(ligne["description"], mots):
                        resultats.append({"instagram": ligne, "quand": ligne["publie_le"],
                                          "extrait": recherche.extrait(ligne["description"], mots)})
                resultats.sort(key=lambda r: r["quand"], reverse=True)
            return page(request, "recherche.html", s, q=q, mots=mots, resultats=resultats[:60],
                        trop_court=bool(q.strip()) and not mots)

    @app.get("/historique")
    def historique(request: Request):
        with ouvrir() as s:
            taches = [t for statut in ("validé", "refusé") for t in s.file.lister(statut=statut)
                      if t.type == "post_brouillon"]
            taches.sort(key=lambda t: t.maj_le, reverse=True)
            publications = Publications(s.file, s.biblio, horloge)
            return page(request, "historique.html", s, taches=taches[:100], modifies=modifications(s).posts_modifies(),
                        publications={t.id: publications.lire(t.id) for t in taches[:100]})

    # --- Corbeille ---

    @app.get("/corbeille")
    def corbeille(request: Request):
        with ouvrir() as s:
            delai = timedelta(hours=config.corbeille_delai_heures)
            elements = [{"asset": a, "suppression": None if a.analyse == "en_attente"
                         else datetime.fromisoformat(a.corbeille_le) + delai}
                        for a in s.biblio.lister("corbeille")]
            return page(request, "corbeille.html", s, elements=elements)

    @app.post("/corbeille/{asset_id}/restaurer")
    def restaurer(asset_id: int):
        with ouvrir() as s:
            try:
                s.biblio.restaurer(asset_id)
            except StockageErreur as erreur:
                return rediriger("/corbeille", erreur=str(erreur))
        return rediriger("/corbeille", info=f"Élément {asset_id} restauré.")

    @app.post("/corbeille/{asset_id}/supprimer")
    def supprimer_sans_analyse(asset_id: int):
        """Choix humain quand le disque est presque plein : supprimer un contenu sans attendre son analyse."""
        with ouvrir() as s:
            try:
                asset = s.biblio.lire(asset_id)
                if asset.etat != "corbeille":
                    return rediriger("/corbeille", erreur=f"L'élément {asset_id} n'est pas dans la corbeille.")
                if asset.analyse == "en_attente" and not s.purge.etat_disque().sous_seuil:
                    return rediriger("/corbeille", erreur="Suppression sans analyse possible seulement quand le "
                                                          "disque passe sous le seuil d'alerte.")
                s.biblio.supprimer(asset_id)
            except StockageErreur as erreur:
                return rediriger("/corbeille", erreur=str(erreur))
        return rediriger("/corbeille", info=f"Élément {asset_id} supprimé définitivement.")

    # --- Leçons ---

    @app.get("/lecons")
    def lecons(request: Request):
        with ouvrir() as s:
            proposees = []
            for lecon in s.lecons.lister("proposée"):
                tache = s.file.lire(lecon.tache_id)
                rejet = s.file.lire(tache.parent_id).contenu if tache.parent_id else {}
                proposees.append({"lecon": lecon, "rappel_de": tache.contenu.get("rappel_de"), "rejet": rejet})
            return page(request, "lecons.html", s, proposees=proposees, validees=s.lecons.lister("validée"))

    @app.post("/lecons/{lecon_id}/valider")
    def valider_lecon(lecon_id: int, regle: str = Form("")):
        with ouvrir() as s:
            try:
                s.lecons.valider(lecon_id, regle)
            except (LeconErreur, TransitionInterdite) as erreur:
                return rediriger("/lecons", erreur=str(erreur))
        return rediriger("/lecons", info=f"Leçon {lecon_id} validée : les agents l'appliquent dès maintenant.")

    @app.post("/lecons/{lecon_id}/refuser")
    def refuser_lecon(lecon_id: int, commentaire: str = Form("")):
        with ouvrir() as s:
            try:
                s.lecons.refuser(lecon_id, commentaire)
            except (LeconErreur, TransitionInterdite) as erreur:
                return rediriger("/lecons", erreur=str(erreur))
        return rediriger("/lecons", info=f"Leçon {lecon_id} refusée.")

    @app.post("/lecons/{lecon_id}/retirer")
    def retirer_lecon(lecon_id: int, retour: str = Form("/lecons")):
        retour = retour if retour in ("/lecons", "/consignes") else "/lecons"
        with ouvrir() as s:
            try:
                s.lecons.retirer(lecon_id)
            except LeconErreur as erreur:
                return rediriger(retour, erreur=str(erreur))
        return rediriger(retour, info=f"Leçon {lecon_id} retirée : les agents ne l'appliquent plus.")

    # --- Consignes : demandes de modification et règles ajoutées par l'humain ---

    def modifications(s: Services) -> Modifications:
        return Modifications(s.conn, s.file, s.biblio, s.lecons, Creneaux.depuis_config(config, s.conn, horloge),
                             horloge)

    @app.get("/consignes")
    def consignes(request: Request):
        with ouvrir() as s:
            posts = [{"tache": t, "medias": medias_de(s, t)} for t in modifications(s).modifiables()]
            regles = [l for l in s.lecons.lister("validée") if l.origine_rejet == "humain"]
            recentes = s.conn.execute("SELECT * FROM modifications ORDER BY id DESC LIMIT 10").fetchall()
            return page(request, "consignes.html", s, posts=posts, regles=list(reversed(regles)),
                        recentes=recentes, agents_regle=AGENTS_REGLE)

    @app.post("/lecons/ajouter")
    def ajouter_regle(regle: str = Form(""), agents: list[str] = Form([])):
        with ouvrir() as s:
            try:
                lecon = s.lecons.ajouter(regle, agents)
            except LeconErreur as erreur:
                return rediriger("/consignes", erreur=str(erreur))
        return rediriger("/consignes", info=f"Règle {lecon.id} ajoutée : les agents l'appliquent dès maintenant.")

    @app.post("/taches/{tache_id}/modifier")
    def modifier_tache(tache_id: int, consignes: str = Form(""), portee: str = Form("texte"),
                       tout_de_suite: str = Form(""), en_regle: str = Form(""), retour: str = Form("/a-valider")):
        retour = retour if retour in ("/a-valider", "/consignes", "/calendrier") else "/a-valider"
        with ouvrir() as s:
            try:
                modification = modifications(s).demander(
                    tache_id, consignes, portee, immediat=bool(tout_de_suite), en_regle=bool(en_regle),
                    agents_regle=AGENTS_REGLE_TEXTE if portee == "texte" else AGENTS_REGLE)
            except (ModificationErreur, TacheIntrouvable) as erreur:
                return rediriger(retour, erreur=str(erreur))
            nuit = s.file.lire(modification.tache_id).creneau == "nuit"
        quand = "cette nuit, prête demain matin" if nuit else "tout de suite"
        regle = f" Elle devient aussi la règle {modification.lecon_id}." if modification.lecon_id else ""
        return rediriger(retour, info=f"Modification du contenu {tache_id} demandée : nouvelle version {quand}.{regle}")

    # --- Pause de l'agence décidée par l'humain ---

    def page_de_retour(request: Request) -> str:
        """Page d'où vient le clic (chemin seulement, jamais une autre adresse)."""
        chemin = urlparse(request.headers.get("referer") or "").path
        return chemin if chemin.startswith("/") and not chemin.startswith("//") else "/"

    @app.post("/pause")
    def mettre_en_pause(request: Request, heures: str = Form("")):
        retour = page_de_retour(request)
        if heures and (not heures.isdigit() or int(heures) not in DUREES_HEURES):
            return rediriger(retour, erreur=f"Durée de pause inconnue : {heures}")
        with ouvrir() as s:
            etat = Pause(s.conn, horloge).activer(timedelta(hours=int(heures)) if heures else None)
        quand = f"jusqu'à {etat.jusqu_a.astimezone(FUSEAU):%H:%M}" if etat.jusqu_a else "jusqu'à ce que vous la repreniez"
        return rediriger(retour, info=f"Agence en pause {quand} : votre abonnement Claude est tout à vous.")

    @app.post("/pause/reprendre")
    def reprendre(request: Request):
        with ouvrir() as s:
            Pause(s.conn, horloge).reprendre()
        return rediriger(page_de_retour(request), info="L'agence reprend le travail.")

    @app.post("/quota/actualiser")
    def actualiser_quota(request: Request):
        retour = request.headers.get("referer") or "/a-valider"
        with ouvrir() as s, dossier_de_travail(config.dossier_temp) as dossier:
            try:
                MoteurClaude(config, s.couts).actualiser_quota(dossier)
            except Exception as erreur:  # mesure de confort : jamais bloquante pour l'interface
                return RedirectResponse(f"{retour.split('?')[0]}?{urlencode({'erreur': f'Mesure impossible : {erreur}'})}",
                                        status_code=303)
        return RedirectResponse(retour.split("?")[0], status_code=303)

    # --- Calendrier et veille ---

    def vue_calendrier(request: Request, s: Services, lundi: date, detail: dict | None = None) -> Response:
        calendrier = Calendrier(s.file, Publications(s.file, s.biblio, horloge))
        maintenant = horloge().astimezone(FUSEAU)
        planning = s.conn.execute("SELECT * FROM plannings WHERE semaine = ? ORDER BY id DESC LIMIT 1",
                                  (lundi.isoformat(),)).fetchone()
        en_cours = [t for t in s.file.lister() if t.type in ("demande_veille", "demande_planning")
                    and t.statut in ("à_faire", "en_cours")]
        jours = calendrier.semaine(lundi, maintenant)
        vignettes = {e.racine.id: medias_de(s, e.post)[:1] if e.post else []
                     for j in jours for e in j.elements if isinstance(e, ElementPublication)}
        return page(request, "calendrier.html", s, publication_automatique=config.publication_automatique, jours=jours, lundi=lundi, aujourdhui=maintenant.date(),
                    precedente=lundi - timedelta(days=7), suivante=lundi + timedelta(days=7),
                    cette_semaine=lundi_de(maintenant.date()), planning=planning, en_cours=en_cours,
                    vignettes=vignettes, detail=detail)

    def lundi_demande(semaine: str | None) -> date:
        try:
            return lundi_de(date.fromisoformat(semaine)) if semaine else lundi_de(horloge().astimezone(FUSEAU).date())
        except ValueError:
            return lundi_de(horloge().astimezone(FUSEAU).date())

    @app.get("/calendrier")
    def calendrier(request: Request, semaine: str | None = None):
        with ouvrir() as s:
            return vue_calendrier(request, s, lundi_demande(semaine))

    @app.get("/calendrier/tache/{tache_id}")
    def calendrier_tache(request: Request, tache_id: int, semaine: str | None = None):
        """Une tâche ouverte en grand par-dessus la semaine : publication complète ou tâche isolée."""
        with ouvrir() as s:
            calendrier = Calendrier(s.file, Publications(s.file, s.biblio, horloge))
            try:
                tache = s.file.lire(tache_id)
            except TacheIntrouvable:
                return rediriger("/calendrier", erreur=f"Tâche {tache_id} introuvable.")
            racine = calendrier.racine_de(tache_id)
            if racine:
                element = calendrier.publication(racine)
                visuels = [t for t in element.lignee if t.type == "asset"]
                detail = {"publication": element, "medias": medias_de(s, element.post) if element.post else [],
                          "conception": visuels[-1].contenu.get("prompt_ou_script") if visuels else None,
                          "reel": bool(visuels) and visuels[-1].contenu.get("type_media") == "vidéo",
                          "statistiques": statistiques(s).du_post(element.post.id) if element.publication else None,
                          "chiffres": CHIFFRES, "libelles": LIBELLES}
                quand = element.quand
            else:
                detail = {"tache": tache}
                quand = datetime.fromisoformat(tache.cree_le).astimezone(FUSEAU)
            return vue_calendrier(request, s, lundi_demande(semaine) if semaine else lundi_de(quand.date()), detail)

    @app.post("/calendrier/preparer")
    def preparer_semaine():
        with ouvrir() as s:
            planificateur = Planificateur(s.conn, s.file, horloge)
            planificateur.lancer("demande_veille")
            planificateur.lancer("demande_planning")
        return rediriger("/calendrier", info="Veille puis planning lancés : la semaine apparaîtra ici une fois prête.")

    @app.get("/veille")
    def veille(request: Request):
        with ouvrir() as s:
            ligne = s.conn.execute("SELECT * FROM rapports_veille ORDER BY id DESC LIMIT 1").fetchone()
            rapport = json.loads(ligne["contenu"]) if ligne else None
            concurrents = s.conn.execute("SELECT * FROM concurrents ORDER BY maj_le DESC").fetchall()
            en_cours = [t for t in s.file.lister(agent="agent_veille") if t.statut in ("à_faire", "en_cours")]
            return page(request, "veille.html", s, rapport=rapport, ligne=ligne, concurrents=concurrents,
                        en_cours=en_cours)

    @app.post("/veille/lancer")
    def lancer_veille():
        with ouvrir() as s:
            Planificateur(s.conn, s.file, horloge).lancer("demande_veille")
        return rediriger("/veille", info="Veille lancée : le rapport apparaîtra ici dans quelques minutes.")

    # --- Statistiques et analyste ---

    @app.get("/statistiques")
    def page_statistiques(request: Request):
        with ouvrir() as s:
            stats = statistiques(s)
            ligne = s.conn.execute("SELECT * FROM rapports_analyste ORDER BY id DESC LIMIT 1").fetchone()
            en_cours = [t for t in s.file.lister(agent="agent_analyste") if t.statut in ("à_faire", "en_cours")]
            return page(request, "statistiques.html", s, lignes=stats.lister(), releve_api=stats.dernier_releve_api(),
                        api=bool(config.instagram_jeton and config.instagram_compte_id), chiffres=CHIFFRES, libelles=LIBELLES,
                        engagement=engagement, rapport=json.loads(ligne["contenu"]) if ligne else None,
                        rapport_le=ligne["cree_le"] if ligne else None, en_cours=en_cours)

    @app.post("/statistiques/importer")
    async def importer_statistiques(fichier: UploadFile):
        contenu = await fichier.read()
        with ouvrir() as s:
            try:
                rapport = statistiques(s).importer_csv(contenu)
            except (StatistiquesErreur, UnicodeDecodeError) as erreur:
                return rediriger("/statistiques", erreur=str(erreur))
        message = f"{rapport.lignes} post(s) importé(s), dont {len(rapport.rattachees)} de l'agence."
        if rapport.erreurs:
            return rediriger("/statistiques", info=message, erreur=" ; ".join(rapport.erreurs[:5]))
        return rediriger("/statistiques", info=message)

    @app.post("/statistiques/relever")
    def relever_statistiques():
        if not (config.instagram_jeton and config.instagram_compte_id):
            return rediriger("/statistiques", erreur="Jeton Instagram non réglé (INSTAGRAM_JETON, INSTAGRAM_COMPTE_ID).")
        client = ClientInstagram(config.instagram_api_url, config.instagram_compte_id, config.instagram_jeton)
        with ouvrir() as s:
            try:
                rapport = statistiques(s).relever_api(client)
            except (ErreurDefinitive, ErreurTemporaire) as erreur:
                return rediriger("/statistiques", erreur=str(erreur))
        return rediriger("/statistiques", info=f"{rapport.lignes} posts relevés depuis Instagram, "
                                               f"dont {len(rapport.rattachees)} de l'agence.")

    @app.post("/statistiques/analyser")
    def analyser_maintenant():
        with ouvrir() as s:
            semaine = lundi_suivant(horloge().astimezone(FUSEAU).date())
            s.file.creer(DemandeAnalyse(semaine=semaine, forcer=True, modele="claude-sonnet-5-5", effort="medium"))
        return rediriger("/statistiques", info="Analyse lancée : le rapport apparaîtra ici dans une minute environ.")

    # --- Réglages ---

    @app.get("/reglages")
    def reglages(request: Request):
        with ouvrir() as s:
            nb_echecs = len(s.file.lister(statut="échec"))
            elements = [
                ("/direction", "sparkle", "Direction artistique", "Le style que suivent le graphiste et le motion designer.",
                 len(s.directions.lister("proposée")), False),
                ("/references", "image-square", "Références", "Vos meilleurs posts, modèles de la direction artistique.", 0, False),
                ("/rushs", "images", "Rushs", "Vos photos, utilisées telles quelles dans les visuels.", 0, False),
                ("/sons", "speaker-high", "Sons", "Les bruitages des reels : à écouter, écartez ceux qui ne vont pas.",
                 0, False),
                ("/historique", "clock-counter-clockwise", "Historique", "Les contenus validés et refusés.", 0, False),
                ("/echecs", "warning-circle", "Tâches en échec", "À relancer après correction.", nb_echecs, True),
                ("/corbeille", "trash", "Corbeille", "Contenus écartés, supprimés après analyse.",
                 len(s.biblio.lister("corbeille")), False),
            ]
            return page(request, "reglages.html", s, elements=elements)

    # --- Tâches en échec ---

    @app.get("/echecs")
    def echecs(request: Request):
        with ouvrir() as s:
            return page(request, "echecs.html", s, taches=s.file.lister(statut="échec"))

    @app.post("/taches/{tache_id}/relancer")
    def relancer(tache_id: int, retour: str = Form("/echecs")):
        # Retour vers la page d'où vient le bouton (Tâches en échec ou fenêtre d'un agent), jamais vers un autre site.
        retour = retour if retour in ("/echecs",) or re.fullmatch(r"/agents/[a-z_]+", retour) else "/echecs"
        with ouvrir() as s:
            try:
                s.file.relancer(tache_id)
            except TransitionInterdite as erreur:
                return rediriger(retour, erreur=str(erreur))
        return rediriger(retour, info=f"Tâche {tache_id} relancée.")

    return app
