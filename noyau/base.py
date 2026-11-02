"""Base SQLite : connexion, transactions et schéma.

Les dates sont écrites par le code (horloge injectable pour les tests), en ISO 8601 UTC.
Le schéma est versionné avec PRAGMA user_version pour les migrations futures.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

STATUTS = ("à_faire", "en_cours", "à_valider", "validé", "refusé", "terminé", "échec")
VOIES = ("normale", "rendu")

VERSION_SCHEMA = 15


def _liste(valeurs: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in valeurs)


SCHEMA = f"""
-- Une tâche = un message typé adressé à un agent (identifiants de docs/architecture.md).
CREATE TABLE taches (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,
    agent       TEXT NOT NULL,
    voie        TEXT NOT NULL DEFAULT 'normale' CHECK (voie IN ({_liste(VOIES)})),
    statut      TEXT NOT NULL DEFAULT 'à_faire' CHECK (statut IN ({_liste(STATUTS)})),
    contenu     TEXT NOT NULL CHECK (json_valid(contenu)),
    parent_id   INTEGER REFERENCES taches(id),
    tentatives  INTEGER NOT NULL DEFAULT 0,
    erreur      TEXT,
    commentaire TEXT,
    cree_le     TEXT NOT NULL,
    maj_le      TEXT NOT NULL,
    pris_le     TEXT
);
CREATE INDEX taches_a_prendre ON taches (statut, agent, voie, cree_le);
CREATE INDEX taches_parent ON taches (parent_id);
-- Garantie au niveau de la base : jamais deux rendus en cours en même temps.
CREATE UNIQUE INDEX un_seul_rendu ON taches (voie) WHERE voie = 'rendu' AND statut = 'en_cours';

-- Rushs fournis par l'humain et médias produits par les agents.
CREATE TABLE assets (
    id               INTEGER PRIMARY KEY,
    origine          TEXT NOT NULL CHECK (origine IN ('rush', 'généré')),
    type             TEXT NOT NULL CHECK (type IN ('image', 'vidéo', 'autre')),
    chemin           TEXT NOT NULL UNIQUE,
    nom_original     TEXT,
    taille_octets    INTEGER NOT NULL CHECK (taille_octets >= 0),
    tache_id         INTEGER REFERENCES taches(id),
    etat             TEXT NOT NULL DEFAULT 'actif' CHECK (etat IN ('actif', 'corbeille', 'supprimé')),
    motif_corbeille  TEXT CHECK (motif_corbeille IN ('refusé', 'ne_pas_utiliser', 'relecteur')),
    -- Analyse par agent_erreurs : les rushs n'en ont pas besoin.
    analyse          TEXT NOT NULL DEFAULT 'non_requise' CHECK (analyse IN ('non_requise', 'en_attente', 'terminée')),
    genere_par_ia    INTEGER NOT NULL DEFAULT 0 CHECK (genere_par_ia IN (0, 1)),
    cree_le          TEXT NOT NULL,
    corbeille_le     TEXT,
    publie_le        TEXT,
    supprime_le      TEXT,
    CHECK ((etat = 'corbeille') = (corbeille_le IS NOT NULL AND motif_corbeille IS NOT NULL))
);
CREATE INDEX assets_etat ON assets (etat, corbeille_le);
CREATE INDEX assets_publication ON assets (publie_le) WHERE publie_le IS NOT NULL;

-- Journal de chaque appel de modèle (coût 0 pour Claude, inclus dans l'abonnement).
CREATE TABLE couts (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,
    fournisseur    TEXT NOT NULL,
    modele         TEXT NOT NULL,
    jetons_entree  INTEGER NOT NULL DEFAULT 0,
    jetons_sortie  INTEGER NOT NULL DEFAULT 0,
    images         INTEGER NOT NULL DEFAULT 0,
    cout_usd       REAL NOT NULL CHECK (cout_usd >= 0),
    tache_id       INTEGER REFERENCES taches(id)
);
CREATE INDEX couts_date ON couts (date);

-- Analyses des contenus rejetés et leçons proposées par agent_erreurs.
-- Seules les leçons validées sont recopiées dans lecons.md.
CREATE TABLE lecons (
    id                INTEGER PRIMARY KEY,
    tache_id          INTEGER NOT NULL REFERENCES taches(id),
    origine_rejet     TEXT NOT NULL CHECK (origine_rejet IN ('refus_humain', 'ne_pas_utiliser', 'relecteur')),
    erreur            TEXT NOT NULL,
    cause             TEXT NOT NULL,
    regle             TEXT NOT NULL,
    agents_concernes  TEXT NOT NULL CHECK (json_valid(agents_concernes)),
    statut            TEXT NOT NULL DEFAULT 'proposée' CHECK (statut IN ('proposée', 'validée', 'refusée')),
    commentaire       TEXT,
    cree_le           TEXT NOT NULL,
    decide_le         TEXT
);
CREATE INDEX lecons_statut ON lecons (statut);
"""


def connecter(chemin: Path) -> sqlite3.Connection:
    """Ouvre la base, crée le dossier et le schéma si besoin.

    Mode autocommit : les écritures groupées passent par `transaction()`.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    _initialiser(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction exclusive en écriture dès le début (BEGIN IMMEDIATE), annulée en cas d'erreur."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# Évolutions du schéma, appliquées dans l'ordre sur une base existante (jamais modifiées une fois livrées).
SCHEMA_V2 = """
-- Références de style : réalisations passées de l'humain (posts Instagram), jamais publiées comme contenu.
CREATE TABLE references_style (
    id             INTEGER PRIMARY KEY,
    chemin         TEXT NOT NULL UNIQUE,
    nom_original   TEXT,
    taille_octets  INTEGER NOT NULL,
    ajoute_le      TEXT NOT NULL
);
"""
SCHEMA_V3 = """
-- Directions artistiques proposées par agent_visuel à partir des références ; seule la dernière validée s'applique.
CREATE TABLE directions_artistiques (
    id           INTEGER PRIMARY KEY,
    tache_id     INTEGER REFERENCES taches(id),
    texte        TEXT NOT NULL,
    resume       TEXT NOT NULL,
    references_json TEXT NOT NULL CHECK (json_valid(references_json)),
    statut       TEXT NOT NULL DEFAULT 'proposée' CHECK (statut IN ('proposée', 'validée', 'refusée', 'remplacée')),
    commentaire  TEXT,
    cree_le      TEXT NOT NULL,
    decide_le    TEXT
);
"""
SCHEMA_V4 = """
-- Quota de l'abonnement Claude rapporté à chaque appel (fenêtres de 5 heures et de 7 jours).
CREATE TABLE quota_abonnement (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,
    fenetre      TEXT NOT NULL,
    utilisation  REAL NOT NULL,
    reprise      TEXT,
    statut       TEXT NOT NULL
);
CREATE INDEX quota_fenetre ON quota_abonnement (fenetre, id);
"""
SCHEMA_V5 = """
-- Planificateur : dernier lancement de chaque tâche périodique.
CREATE TABLE planification (
    evenement         TEXT PRIMARY KEY,
    dernier_lancement TEXT NOT NULL
);
-- Concurrents identifiés par la veille, mémorisés d'une semaine sur l'autre.
CREATE TABLE concurrents (
    id            INTEGER PRIMARY KEY,
    nom           TEXT NOT NULL UNIQUE,
    site          TEXT,
    zone          TEXT,
    ce_qui_marche TEXT NOT NULL,
    decouvert_le  TEXT NOT NULL,
    maj_le        TEXT NOT NULL
);
-- Rapports de veille (contenu du message rapport_veille) et plannings hebdomadaires de l'orchestrateur.
CREATE TABLE rapports_veille (
    id        INTEGER PRIMARY KEY,
    tache_id  INTEGER REFERENCES taches(id),
    semaine   TEXT NOT NULL,
    contenu   TEXT NOT NULL CHECK (json_valid(contenu)),
    cree_le   TEXT NOT NULL
);
CREATE TABLE plannings (
    id         INTEGER PRIMARY KEY,
    tache_id   INTEGER REFERENCES taches(id),
    semaine    TEXT NOT NULL,
    strategie  TEXT NOT NULL,
    cree_le    TEXT NOT NULL
);
"""
SCHEMA_V6 = """
-- Publications : un contenu validé publié (à la main ou par l'API), une seule fois.
CREATE TABLE publications (
    id                  INTEGER PRIMARY KEY,
    tache_id            INTEGER NOT NULL UNIQUE REFERENCES taches(id),
    mode                TEXT NOT NULL CHECK (mode IN ('manuel', 'instagram')),
    identifiant_externe TEXT,
    publie_le           TEXT NOT NULL
);
"""
SCHEMA_V7 = """
-- Lien public du post publié par l'API (permalink Instagram).
ALTER TABLE publications ADD COLUMN lien TEXT;
"""
SCHEMA_V8 = """
-- Statistiques des posts Instagram (saisies, importées du CSV de Meta Business Suite, ou lues par l'API).
-- post_id : post de l'agence, s'il a été rattaché ; sinon, historique du compte.
CREATE TABLE statistiques (
    id                    INTEGER PRIMARY KEY,
    identifiant_instagram TEXT UNIQUE,
    post_id               INTEGER UNIQUE REFERENCES taches(id),
    publie_le             TEXT,
    type                  TEXT,
    description           TEXT,
    lien                  TEXT,
    duree_s               INTEGER,
    vues                  INTEGER,
    couverture            INTEGER,
    jaime                 INTEGER,
    commentaires          INTEGER,
    partages              INTEGER,
    enregistrements       INTEGER,
    abonnes               INTEGER,
    source                TEXT NOT NULL CHECK (source IN ('manuel', 'csv', 'api')),
    releve_le             TEXT NOT NULL
);
-- Rapports hebdomadaires de l'analyste (contenu du message rapport).
CREATE TABLE rapports_analyste (
    id        INTEGER PRIMARY KEY,
    tache_id  INTEGER REFERENCES taches(id),
    contenu   TEXT NOT NULL CHECK (json_valid(contenu)),
    cree_le   TEXT NOT NULL
);
"""
SCHEMA_V9 = """
-- Créneau de production de chaque tâche (noyau/creneaux.py) : jour, nuit, ou suite d'une chaîne de nuit.
ALTER TABLE taches ADD COLUMN creneau TEXT NOT NULL DEFAULT 'jour' CHECK (creneau IN ('jour', 'nuit', 'nuit_suite'));
"""
SCHEMA_V10 = """
-- Crédits des API à crédits (ElevenLabs : 1 par caractère de voix, 200 par bruitage).
ALTER TABLE couts ADD COLUMN credits INTEGER NOT NULL DEFAULT 0;
-- Bibliothèque de bruitages générés, réutilisés d'une vidéo à l'autre pour économiser les crédits.
CREATE TABLE bruitages (
    id            INTEGER PRIMARY KEY,
    description   TEXT NOT NULL,
    duree         REAL NOT NULL,
    chemin        TEXT NOT NULL UNIQUE,
    utilisations  INTEGER NOT NULL DEFAULT 0,
    cree_le       TEXT NOT NULL
);
"""
SCHEMA_V11 = """
-- Voix et bruitages abandonnés (ElevenLabs, décision de l'humain) : bibliothèque de bruitages et crédits retirés.
DROP TABLE bruitages;
ALTER TABLE couts DROP COLUMN credits;
"""
SCHEMA_V12 = """
-- Notifications push déjà envoyées (un événement n'est notifié qu'une fois).
CREATE TABLE notifications (
    type       TEXT NOT NULL,
    objet_id   INTEGER NOT NULL,
    envoye_le  TEXT NOT NULL,
    PRIMARY KEY (type, objet_id)
);
"""
SCHEMA_V13 = """
-- Nombre d'abonnés du compte Instagram, noté à chaque relevé des statistiques (croissance du compte).
CREATE TABLE abonnes (
    date    TEXT PRIMARY KEY,
    nombre  INTEGER NOT NULL
);
-- Aperçu du post : adresse temporaire du CDN de Meta, renouvelée à chaque relevé.
ALTER TABLE statistiques ADD COLUMN apercu TEXT;
"""
SCHEMA_V14 = """
-- Banque de bruitages (noyau/banque_sons.py) : vrais enregistrements CC0 trouvés sur Openverse (Freesound).
-- La ligne d'un son supprimé reste (chemin vide) pour qu'un nouvel import ne le reprenne pas.
CREATE TABLE sons (
    id            INTEGER PRIMARY KEY,
    openverse_id  TEXT NOT NULL UNIQUE,
    categorie     TEXT NOT NULL,
    titre         TEXT NOT NULL,
    mots_cles     TEXT NOT NULL DEFAULT '',
    auteur        TEXT NOT NULL DEFAULT '',
    page          TEXT NOT NULL,
    licence_url   TEXT NOT NULL,
    chemin        TEXT UNIQUE,
    duree         REAL NOT NULL,
    attaque       REAL NOT NULL,
    etat          TEXT NOT NULL DEFAULT 'actif' CHECK (etat IN ('actif', 'corbeille', 'supprimé')),
    corbeille_le  TEXT,
    cree_le       TEXT NOT NULL
);
"""
SCHEMA_V15 = """
-- Règles ajoutées spontanément par l'humain : leçons d'origine « humain », sans tâche ni contenu d'origine.
-- SQLite ne modifie pas une contrainte CHECK : la table est recréée (aucune autre table ne la référence).
CREATE TABLE lecons_v15 (
    id                INTEGER PRIMARY KEY,
    tache_id          INTEGER REFERENCES taches(id),
    origine_rejet     TEXT NOT NULL CHECK (origine_rejet IN ('refus_humain', 'ne_pas_utiliser', 'relecteur', 'humain')),
    erreur            TEXT NOT NULL,
    cause             TEXT NOT NULL,
    regle             TEXT NOT NULL,
    agents_concernes  TEXT NOT NULL CHECK (json_valid(agents_concernes)),
    statut            TEXT NOT NULL DEFAULT 'proposée' CHECK (statut IN ('proposée', 'validée', 'refusée')),
    commentaire       TEXT,
    cree_le           TEXT NOT NULL,
    decide_le         TEXT
);
INSERT INTO lecons_v15 SELECT id, tache_id, origine_rejet, erreur, cause, regle, agents_concernes, statut,
    commentaire, cree_le, decide_le FROM lecons;
DROP TABLE lecons;
ALTER TABLE lecons_v15 RENAME TO lecons;
CREATE INDEX lecons_statut ON lecons (statut);
-- Demandes de modification de l'humain (noyau/modifications.py) : le post d'origine est remplacé par une
-- nouvelle version, sans être compté comme un refus ni analysé pour en tirer une leçon.
CREATE TABLE modifications (
    id         INTEGER PRIMARY KEY,
    post_id    INTEGER NOT NULL UNIQUE REFERENCES taches(id),
    tache_id   INTEGER NOT NULL REFERENCES taches(id),
    consignes  TEXT NOT NULL,
    portee     TEXT NOT NULL CHECK (portee IN ('texte', 'tout')),
    lecon_id   INTEGER REFERENCES lecons(id),
    cree_le    TEXT NOT NULL
);
"""
MIGRATIONS = {1: SCHEMA, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA_V5, 6: SCHEMA_V6, 7: SCHEMA_V7,
              8: SCHEMA_V8, 9: SCHEMA_V9, 10: SCHEMA_V10, 11: SCHEMA_V11, 12: SCHEMA_V12,
              13: SCHEMA_V13, 14: SCHEMA_V14, 15: SCHEMA_V15}


def _initialiser(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # executescript() validerait la transaction en cours : on exécute instruction par instruction.
    for cible in range(version + 1, VERSION_SCHEMA + 1):
        with transaction(conn):
            for instruction in _instructions(MIGRATIONS[cible]):
                conn.execute(instruction)
            conn.execute(f"PRAGMA user_version = {cible}")


def _instructions(script: str) -> list[str]:
    lignes = [l for l in script.splitlines() if not l.strip().startswith("--")]
    return [i.strip() for i in "\n".join(lignes).split(";") if i.strip()]
