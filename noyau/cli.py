"""Ligne de commande de l'agence : `agence <commande>` (ou `python -m noyau.cli <commande>`)."""

import argparse
import getpass
import json
import logging
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

from noyau import banc_video, sauvegarde
from noyau.banque_sons import PAR_CATEGORIE, Banque, BanqueErreur
from noyau.instagram import ClientInstagram
from noyau.notifications import Notifications
from noyau.publication import Publications
from noyau.statistiques import Statistiques
from noyau.base import connecter
from noyau.config import ConfigErreur, charger, ecrire_env
from noyau.contexte import CharteErreur, lire
from noyau.erreurs import ErreurDefinitive, ErreurTemporaire
from noyau.file import File
from noyau.messages import MESSAGES, MessageInvalide, valider
from noyau.purge import Purge
from noyau.stockage import GO, Bibliotheque, StockageErreur


class Erreur(Exception):
    """Erreur affichée telle quelle à l'utilisateur, sans trace technique."""


def taille(octets: int) -> str:
    for unite, facteur in (("Go", GO), ("Mo", 1024**2), ("Ko", 1024)):
        if octets >= facteur:
            return f"{octets / facteur:.1f} {unite}"
    return f"{octets} o"


def date_courte(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y %H:%M") if iso else "-"


def tableau(entetes: list[str], lignes: list[list[str]]) -> None:
    if not lignes:
        print("(vide)")
        return
    largeurs = [max(len(str(x)) for x in colonne) for colonne in zip(entetes, *lignes)]
    for ligne in [entetes, *lignes]:
        print("  ".join(str(x).ljust(l) for x, l in zip(ligne, largeurs)).rstrip())


class Commandes:
    def __init__(self):
        self.config = charger()
        self.conn = connecter(self.config.chemin_base)
        self.biblio = Bibliotheque.depuis_config(self.conn, self.config)
        self.file = File(self.conn)
        self.purge = Purge.depuis_config(self.biblio, self.config)

    # --- init / disque / purge ---

    def init(self, args) -> None:
        for dossier in (self.config.dossier_contenus, self.config.dossier_temp):
            dossier.mkdir(parents=True, exist_ok=True)
        print(f"Base prête : {self.config.chemin_base}")
        print(f"Contenus : {self.config.dossier_contenus}")
        print(f"Fichiers de travail : {self.config.dossier_temp}")
        try:
            lire()
            print("Charte : prête")
        except CharteErreur as erreur:
            print(f"Charte : {erreur}")
        self.disque(args)

    def disque(self, args) -> None:
        etat = self.purge.etat_disque()
        print(f"Espace libre : {taille(etat.libre_octets)} "
              f"(alerte sous {self.config.disque_seuil_alerte_go:g} Go, ajout refusé sous {self.config.disque_seuil_refus_go:g} Go)")
        print(f"Contenus en corbeille en attente d'analyse : {len(etat.contenus_non_analyses)}")
        if etat.alerte:
            print(f"ALERTE : {etat.alerte}")

    def lancer_purge(self, args) -> None:
        rapport = self.purge.executer()
        print(f"Corbeille : {len(rapport.corbeille)} élément(s) supprimé(s)")
        print(f"Publiés depuis plus de {self.config.publication_delai_jours:g} jours : {len(rapport.publies)} supprimé(s)")
        print(f"Gardés en attente d'analyse : {len(rapport.prolonges)}")
        print(f"Sons écartés supprimés : {len(rapport.sons)}")
        print(f"Espace libéré : {taille(rapport.octets_liberes)}")
        for asset_id, erreur in rapport.erreurs.items():
            print(f"Erreur sur l'asset {asset_id} : {erreur}")
        if rapport.disque.alerte:
            print(f"ALERTE : {rapport.disque.alerte}")
        if rapport.erreurs:
            raise Erreur(f"{len(rapport.erreurs)} suppression(s) en échec")

    # --- rushs ---

    def rush_ajouter(self, args) -> None:
        echecs = 0
        for fichier in args.fichiers:
            try:
                asset = self.biblio.lire(self.biblio.ajouter(Path(fichier).expanduser(), "rush"))
                print(f"Ajouté : {asset.id}  {asset.nom_original}  ({asset.type}, {taille(asset.taille_octets)})")
            except StockageErreur as erreur:
                echecs += 1
                print(f"Refusé : {fichier} : {erreur}", file=sys.stderr)
        if echecs:
            raise Erreur(f"{echecs} fichier(s) non ajouté(s)")

    def rush_lister(self, args) -> None:
        rushs = self.biblio.lister("actif", origine="rush")
        tableau(["ID", "TYPE", "NOM", "TAILLE", "AJOUTÉ LE"],
                [[a.id, a.type, a.nom_original or "-", taille(a.taille_octets), date_courte(a.cree_le)] for a in rushs])

    def rush_ecarter(self, args) -> None:
        asset = self.biblio.lire(args.id)
        if asset.origine != "rush":
            raise Erreur(f"L'asset {args.id} n'est pas un rush. Les contenus produits s'écartent depuis l'interface, "
                         "pour qu'ils soient analysés.")
        self.biblio.mettre_en_corbeille(args.id, "ne_pas_utiliser")
        print(f"Rush {args.id} mis en corbeille : suppression définitive dans "
              f"{self.config.corbeille_delai_heures:g} h, sauf restauration.")

    # --- corbeille ---

    def corbeille_lister(self, args) -> None:
        delai = timedelta(hours=self.config.corbeille_delai_heures)
        lignes = []
        for a in self.biblio.lister("corbeille"):
            if a.analyse == "en_attente":
                suppression = "après analyse"
            else:
                suppression = date_courte((datetime.fromisoformat(a.corbeille_le) + delai).isoformat())
            lignes.append([a.id, a.origine, a.nom_original or "-", a.motif_corbeille, date_courte(a.corbeille_le), suppression])
        tableau(["ID", "ORIGINE", "NOM", "MOTIF", "EN CORBEILLE DEPUIS", "SUPPRESSION"], lignes)

    def corbeille_restaurer(self, args) -> None:
        self.biblio.restaurer(args.id)
        print(f"Asset {args.id} restauré.")

    # --- tâches ---

    def tache_creer(self, args) -> None:
        brut = Path(args.fichier).read_text(encoding="utf-8") if args.fichier else args.json
        try:
            donnees = json.loads(brut)
        except json.JSONDecodeError as erreur:
            raise Erreur(f"JSON invalide : {erreur}") from None
        message = valider(args.type, donnees)
        try:
            tache_id = self.file.creer(message, agent=args.agent)
        except ValueError as erreur:
            raise Erreur(str(erreur)) from None
        tache = self.file.lire(tache_id)
        print(f"Tâche {tache.id} créée : {tache.type} → {tache.agent} ({tache.statut}, voie {tache.voie})")

    def tache_lister(self, args) -> None:
        taches = self.file.lister(statut=args.statut, agent=args.agent)
        tableau(["ID", "TYPE", "AGENT", "STATUT", "VOIE", "ESSAIS", "MIS À JOUR", "ERREUR / COMMENTAIRE"],
                [[t.id, t.type, t.agent, t.statut, t.voie, t.tentatives, date_courte(t.maj_le),
                  (t.erreur or t.commentaire or "")[:60]] for t in taches])


    # --- Statistiques ---

    def statistiques_releve(self, args) -> None:
        if not (self.config.instagram_jeton and self.config.instagram_compte_id):
            raise Erreur("INSTAGRAM_JETON et INSTAGRAM_COMPTE_ID doivent être réglés dans .env")
        client = ClientInstagram(self.config.instagram_api_url, self.config.instagram_compte_id,
                                 self.config.instagram_jeton)
        statistiques = Statistiques(self.file, Publications(self.file, self.biblio))
        rapport = statistiques.relever_api(client)
        print(f"{rapport.lignes} posts relevés, dont {len(rapport.rattachees)} de l'agence.")
        for erreur in rapport.erreurs:
            print(f"  {erreur}")

    # --- Notifications ---

    def notification_sujet(self, args) -> None:
        sujet = f"noctua-{secrets.token_urlsafe(18)}"
        ecrire_env({"NTFY_SUJET": sujet})
        print(f"Sujet écrit dans .env : {sujet}")
        print("Sur le téléphone : installer l'app ntfy, « S'abonner à un sujet », saisir ce sujet "
              f"(serveur : {self.config.ntfy_serveur}). Le garder secret : qui le connaît peut lire les notifications.")
        print("Relancer ensuite le worker, puis : agence notification test")

    def notification_test(self, args) -> None:
        notifications = Notifications(self.config, self.conn)
        if not notifications.actives:
            raise Erreur("NTFY_SUJET vide : lancer d'abord « agence notification sujet »")
        if not notifications.envoyer(self.config.nom_agence, "Les notifications fonctionnent.", tags=("tada",)):
            raise Erreur(f"Envoi impossible vers {self.config.ntfy_serveur}")
        print("Notification envoyée.")

    # --- Sauvegarde ---

    def sauvegarde(self, args) -> None:
        fichier = sauvegarde.sauvegarder(self.config)
        print(f"Sauvegarde : {fichier} ({taille(fichier.stat().st_size)})")
        for ancienne in sauvegarde.lister(self.config.dossier_sauvegardes)[:-1]:
            print(f"  gardée : {ancienne.name}")

    # --- Vidéo ---

    def video_banc(self, args) -> None:
        banc_video.lancer(args.durees)

    # --- banque de bruitages ---

    def _banque(self) -> Banque:
        return Banque(self.conn, self.biblio.stockage, self.config.dossier_temp)

    def sons_importer(self, args) -> None:
        print("Recherche sur Openverse (CC0 uniquement, licence revérifiée sur Freesound)…")
        try:
            ajoutes = self._banque().importer(args.par_categorie, int(self.config.disque_seuil_refus_go * GO))
        except BanqueErreur as erreur:
            raise Erreur(str(erreur)) from None
        print(f"{ajoutes} son(s) ajouté(s). Écoutez-les dans l'interface (Bibliothèque > Sons) et écartez ceux "
              "qui ne conviennent pas ; relancez l'import pour compléter.")

    def sons_lister(self, args) -> None:
        tableau(["ID", "CATÉGORIE", "DURÉE", "FRAPPE", "TITRE", "AUTEUR"],
                [[s.id, s.categorie, f"{s.duree:g} s", f"{s.attaque:g} s", s.titre[:40], s.auteur]
                 for s in self._banque().lister()])

    def sons_ecarter(self, args) -> None:
        try:
            son = self._banque().ecarter(args.id)
        except BanqueErreur as erreur:
            raise Erreur(str(erreur)) from None
        print(f"Son {son.id} écarté : suppression dans {self.config.corbeille_delai_heures:g} h, sauf restauration.")

    def sons_restaurer(self, args) -> None:
        try:
            son = self._banque().restaurer(args.id)
        except BanqueErreur as erreur:
            raise Erreur(str(erreur)) from None
        print(f"Son {son.id} : {son.etat}")


def definir_mot_de_passe() -> None:
    """Demande le mot de passe deux fois et l'enregistre (haché) dans .env, avec un nouveau secret de session."""
    from app.auth import LONGUEUR_MINIMALE, hacher, nouveau_secret

    mot_de_passe = getpass.getpass("Nouveau mot de passe de l'interface : ")
    if len(mot_de_passe) < LONGUEUR_MINIMALE:
        raise Erreur(f"Le mot de passe doit faire au moins {LONGUEUR_MINIMALE} caractères.")
    if getpass.getpass("Confirmez le mot de passe : ") != mot_de_passe:
        raise Erreur("Les deux saisies sont différentes.")
    # Nouveau secret : les sessions ouvertes avec l'ancien mot de passe sont fermées.
    ecrire_env({"MOT_DE_PASSE_INTERFACE": hacher(mot_de_passe), "SECRET_SESSION": nouveau_secret()})
    print("Mot de passe enregistré dans .env. Redémarrez l'interface (`agence web`) s'il tourne déjà.")


def lancer_web() -> None:
    import uvicorn

    from app.web import creer_app

    config = charger()
    if not config.mot_de_passe_interface or not config.secret_session:
        raise Erreur("Aucun mot de passe défini : lancez d'abord `agence mot-de-passe`.")
    print(f"Interface sur http://{config.hote_web}:{config.port_web} (Ctrl+C pour arrêter)")
    uvicorn.run(creer_app(config), host=config.hote_web, port=config.port_web, log_level="warning")


def analyseur() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agence", description="Agence IA : commandes d'administration")
    sous = parser.add_subparsers(dest="commande", required=True, metavar="commande")

    sous.add_parser("init", help="crée la base et les dossiers, vérifie la charte").set_defaults(action="init")
    sous.add_parser("disque", help="espace disque et alertes").set_defaults(action="disque")
    sous.add_parser("purge", help="supprime la corbeille échue et les contenus publiés anciens").set_defaults(action="lancer_purge")
    sous.add_parser("mot-de-passe", help="définit le mot de passe de l'interface web").set_defaults(action="mot_de_passe")
    sous.add_parser("web", help="lance l'interface web").set_defaults(action="web")

    rush = sous.add_parser("rush", help="bibliothèque de rushs").add_subparsers(dest="sous_commande", required=True, metavar="action")
    p = rush.add_parser("ajouter", help="ajoute une ou plusieurs photos ou vidéos")
    p.add_argument("fichiers", nargs="+")
    p.set_defaults(action="rush_ajouter")
    rush.add_parser("lister", help="liste les rushs disponibles").set_defaults(action="rush_lister")
    p = rush.add_parser("ecarter", help="met un rush en corbeille (« ne pas utiliser »)")
    p.add_argument("id", type=int)
    p.set_defaults(action="rush_ecarter")

    corbeille = sous.add_parser("corbeille", help="corbeille").add_subparsers(dest="sous_commande", required=True, metavar="action")
    corbeille.add_parser("lister", help="liste la corbeille").set_defaults(action="corbeille_lister")
    p = corbeille.add_parser("restaurer", help="sort un élément de la corbeille")
    p.add_argument("id", type=int)
    p.set_defaults(action="corbeille_restaurer")

    stats = sous.add_parser("statistiques", help="statistiques des posts Instagram").add_subparsers(
        dest="sous_commande", required=True, metavar="action")
    stats.add_parser("releve", help="relève les statistiques des derniers posts par l'API (lectures seules)").set_defaults(
        action="statistiques_releve")

    notif = sous.add_parser("notification", help="notifications push (ntfy)").add_subparsers(
        dest="sous_commande", required=True, metavar="action")
    notif.add_parser("sujet", help="crée un sujet ntfy secret et l'écrit dans .env").set_defaults(action="notification_sujet")
    notif.add_parser("test", help="envoie une notification de test").set_defaults(action="notification_test")

    sous.add_parser("sauvegarde", help="sauvegarde compressée de la base (gardée : SAUVEGARDES_CONSERVEES)").set_defaults(
        action="sauvegarde")

    vid = sous.add_parser("video", help="rendu des reels").add_subparsers(dest="sous_commande", required=True,
                                                                         metavar="action")
    p = vid.add_parser("banc", help="banc de test du rendu : temps et mémoire maximale (à lancer sur le VPS)")
    p.add_argument("--durees", type=int, nargs="+", default=[15, 30, 60], help="durées testées, en secondes")
    p.set_defaults(action="video_banc")

    son = sous.add_parser("sons", help="banque de bruitages CC0 (Openverse)").add_subparsers(
        dest="sous_commande", required=True, metavar="action")
    p = son.add_parser("importer", help="complète la banque depuis Openverse (relançable)")
    p.add_argument("--par-categorie", type=int, default=PAR_CATEGORIE, help="sons par catégorie")
    p.set_defaults(action="sons_importer")
    son.add_parser("lister", help="liste les sons utilisables").set_defaults(action="sons_lister")
    for nom, aide in (("ecarter", "écarte un son (corbeille, supprimé après le délai)"),
                      ("restaurer", "sort un son de la corbeille")):
        p = son.add_parser(nom, help=aide)
        p.add_argument("id", type=int)
        p.set_defaults(action=f"sons_{nom}")

    tache = sous.add_parser("tache", help="file de tâches").add_subparsers(dest="sous_commande", required=True, metavar="action")
    p = tache.add_parser("creer", help="ajoute un message à la file")
    p.add_argument("type", choices=sorted(MESSAGES))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="contenu du message en JSON")
    source.add_argument("--fichier", help="fichier JSON contenant le message")
    p.add_argument("--agent", help="destinataire, si plusieurs sont possibles")
    p.set_defaults(action="tache_creer")
    p = tache.add_parser("lister", help="liste les tâches")
    p.add_argument("--statut")
    p.add_argument("--agent")
    p.set_defaults(action="tache_lister")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = analyseur().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        if args.action == "mot_de_passe":
            definir_mot_de_passe()
            return 0
        if args.action == "web":
            lancer_web()
            return 0
        commandes = Commandes()
        try:
            getattr(commandes, args.action)(args)
        finally:
            commandes.conn.close()
    except (Erreur, ConfigErreur, StockageErreur, MessageInvalide, CharteErreur, ErreurDefinitive,
            ErreurTemporaire) as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
