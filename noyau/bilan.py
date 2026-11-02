"""Bilan d'une semaine (lundi → dimanche, heure de Paris) : ce que l'agence a fait et ce que le compte y a gagné.

Lu sur l'accueil le lundi et le mardi, et envoyé en notification le lundi matin (noyau/notifications.py).
"""

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

FUSEAU = ZoneInfo("Europe/Paris")


def bornes(lundi: date) -> tuple[str, str]:
    """Début et fin de la semaine en UTC ISO (comparables aux dates stockées en base)."""
    debut = datetime.combine(lundi, time(0), FUSEAU).astimezone(UTC)
    return debut.isoformat(), (debut + timedelta(days=7)).isoformat()


def semaine_ecoulee(maintenant: datetime) -> date:
    """Lundi de la semaine qui vient de se terminer."""
    aujourdhui = maintenant.astimezone(FUSEAU).date()
    return aujourdhui - timedelta(days=aujourdhui.weekday() + 7)


def bilan(conn: sqlite3.Connection, lundi: date) -> dict:
    debut, fin = bornes(lundi)
    publies = conn.execute("SELECT count(*) FROM statistiques WHERE publie_le >= ? AND publie_le < ?", (debut, fin)).fetchone()[0]
    # Posts de l'agence publiés mais pas encore relevés : comptés aussi (sans doublon avec le relevé).
    publies += conn.execute(
        "SELECT count(*) FROM publications p WHERE p.publie_le >= ? AND p.publie_le < ? "
        "AND NOT EXISTS (SELECT 1 FROM statistiques s WHERE s.post_id = p.tache_id)", (debut, fin)).fetchone()[0]
    # Abonnés : dernier relevé de la semaine moins le dernier relevé d'avant (ou le premier de la semaine).
    avant = conn.execute("SELECT nombre FROM abonnes WHERE date < ? ORDER BY date DESC LIMIT 1", (debut,)).fetchone()
    dans = conn.execute("SELECT nombre FROM abonnes WHERE date >= ? AND date < ? ORDER BY date", (debut, fin)).fetchall()
    reference = avant or (dans[0] if len(dans) > 1 else None)
    abonnes = dans[-1]["nombre"] - reference["nombre"] if dans and reference else None
    lecons = conn.execute("SELECT count(*) FROM lecons WHERE statut = 'validée' AND decide_le >= ? AND decide_le < ?",
                          (debut, fin)).fetchone()[0]
    taches = conn.execute(
        "SELECT sum(statut IN ('terminé', 'validé', 'refusé')) AS faites, sum(statut = 'échec') AS echecs FROM taches "
        "WHERE agent != 'humain' AND maj_le >= ? AND maj_le < ?", (debut, fin)).fetchone()
    abonnement = conn.execute("SELECT max(utilisation) FROM quota_abonnement WHERE fenetre = 'seven_day' "
                              "AND date >= ? AND date < ?", (debut, fin)).fetchone()[0]
    meilleur = conn.execute("SELECT * FROM statistiques WHERE publie_le >= ? AND publie_le < ? "
                            "ORDER BY coalesce(vues, 0) DESC LIMIT 1", (debut, fin)).fetchone()
    return {"lundi": lundi, "publies": publies, "abonnes": abonnes, "lecons": lecons,
            "taches": taches["faites"] or 0, "echecs": taches["echecs"] or 0,
            "abonnement": round(abonnement * 100) if abonnement is not None else None, "meilleur": meilleur}


def resume(b: dict) -> str:
    """Une phrase pour la notification."""
    morceaux = [f"{b['publies']} post{'s' if b['publies'] > 1 else ''} publié{'s' if b['publies'] > 1 else ''}"]
    if b["abonnes"] is not None:
        morceaux.append(f"{b['abonnes']:+d} abonné{'s' if abs(b['abonnes']) > 1 else ''}")
    morceaux.append(f"{b['lecons']} leçon{'s' if b['lecons'] > 1 else ''} retenue{'s' if b['lecons'] > 1 else ''}")
    if b["abonnement"] is not None:
        morceaux.append(f"abonnement à {b['abonnement']} %")
    if b["echecs"]:
        morceaux.append(f"{b['echecs']} échec{'s' if b['echecs'] > 1 else ''}")
    return f"Semaine du {b['lundi']:%d/%m} : " + ", ".join(morceaux) + "."
