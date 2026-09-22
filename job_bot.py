#!/usr/bin/env python3
"""
Bot d'alerte emploi — Indeed, LinkedIn, HelloWork
==================================================

Interroge périodiquement les pages de résultats publiques d'Indeed, LinkedIn
et HelloWork pour plusieurs "familles" de postes, et envoie une notification
Discord dès qu'une offre nouvelle apparaît.

Aucune connexion à un compte perso n'est nécessaire : Indeed et LinkedIn
sont interrogés via la librairie python-jobspy (résultats de recherche
publics), HelloWork via un scraping léger de sa page de résultats publique.

Ce script est fait pour tourner via GitHub Actions (voir
.github/workflows/job-alerts.yml), toutes les ~15 minutes, sans laisser
d'ordinateur allumé.

Variable d'environnement attendue :
    DISCORD_WEBHOOK_URL   URL du webhook Discord à notifier
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

warnings.filterwarnings("ignore")

# --------------------------------------------------------------------------
# Configuration — à ajuster librement sans toucher au reste du script
# --------------------------------------------------------------------------

LOCATION = "Paris, France"          # utilisé pour Indeed / LinkedIn (jobspy)
HELLOWORK_LOCATION = "Paris"        # utilisé pour l'URL de recherche HelloWork
HOURS_OLD = 72                      # ne considérer que les offres postées récemment
RESULTS_PER_SEARCH = 20             # nb de résultats à examiner par recherche

# Chaque "famille" = un ou plusieurs termes de recherche regroupés sous un
# même libellé. Ajoute / retire des termes ou des familles ici si besoin.
# Les familles listées dans PRIORITY_CATEGORIES ressortent en priorité
# (couleur dorée + en tête) dans les notifications Discord.
CATEGORIES: dict[str, list[str]] = {
    # --- domaine culturel / créatif : profil idéal (CV) ---
    "Médiation culturelle": ["médiation culturelle", "médiation numérique"],
    "Communication / réseaux sociaux": [
        "chargé de communication",
        "community manager",
        "chargé de communication digitale",
    ],
    "Événementiel": [
        "chargé événementiel",
        "hôte hôtesse événementiel",
        "régie événementielle",
    ],
    "Audiovisuel / photo / graphisme": ["monteur vidéo", "photographe", "graphiste"],
    # --- job alimentaire : filet de sécurité ---
    "Vente": ["vendeur vendeuse", "conseiller de vente"],
    "Accueil / réception": ["accueil réception", "hôte hôtesse d'accueil"],
    "Service client": ["service client", "employé polyvalent"],
    "Restauration": ["restauration", "équipier restauration"],
}

PRIORITY_CATEGORIES = {
    "Médiation culturelle",
    "Communication / réseaux sociaux",
    "Événementiel",
    "Audiovisuel / photo / graphisme",
}

# Sites à interroger via jobspy (Indeed et LinkedIn scrapent les pages de
# résultats publiques, sans connexion). HelloWork est géré séparément
# plus bas car jobspy ne le supporte pas.
JOBSPY_SITES = ["indeed", "linkedin"]

DATA_DIR = Path(__file__).parent / "data"
SEEN_FILE = DATA_DIR / "seen_jobs.json"
FAILURE_LOG_FILE = DATA_DIR / "failure_counts.json"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

COLOR_PRIORITY = 0xF1C40F   # doré — domaine culturel / CV
COLOR_DEFAULT = 0x5865F2    # blurple Discord — job alimentaire


@dataclass
class JobPosting:
    source: str          # "Indeed" / "LinkedIn" / "HelloWork"
    category: str        # libellé de la famille (ex: "Médiation culturelle")
    job_id: str          # identifiant stable pour dédoublonner (souvent l'URL)
    title: str
    company: str = ""
    location: str = ""
    date_posted: str = ""
    url: str = ""

    @property
    def is_priority(self) -> bool:
        return self.category in PRIORITY_CATEGORIES


# --------------------------------------------------------------------------
# Persistance (déjà-vus / compteurs d'échecs)
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def send_discord_text(content: str) -> None:
    """Message texte simple (bootstrap, avertissements)."""
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL manquant — message non envoyé:")
        print(content)
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
        if r.status_code not in (200, 204):
            print(f"[WARN] Échec envoi Discord ({r.status_code}): {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[WARN] Erreur réseau Discord: {e}")


def send_discord_embeds(embeds: list[dict]) -> None:
    """Envoie des offres sous forme de embeds Discord, par lots de 10 max
    (limite imposée par l'API Discord pour un même message)."""
    if not DISCORD_WEBHOOK_URL:
        print(f"[WARN] DISCORD_WEBHOOK_URL manquant — {len(embeds)} offre(s) non envoyée(s).")
        return
    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=15)
            if r.status_code not in (200, 204):
                print(f"[WARN] Échec envoi Discord ({r.status_code}): {r.text[:200]}")
        except requests.RequestException as e:
            print(f"[WARN] Erreur réseau Discord: {e}")
        time.sleep(1)  # respecte le rate-limit du webhook


def build_embed(job: JobPosting) -> dict:
    fields = [
        {"name": "Catégorie", "value": job.category, "inline": True},
        {"name": "Source", "value": job.source, "inline": True},
    ]
    if job.date_posted:
        fields.append({"name": "Publié", "value": job.date_posted, "inline": True})
    description = job.company or ""
    if job.location:
        description = f"{description} · {job.location}" if description else job.location
    return {
        "title": (f"⭐ {job.title}" if job.is_priority else job.title)[:256],
        "url": job.url or None,
        "description": description[:2048] if description else None,
        "color": COLOR_PRIORITY if job.is_priority else COLOR_DEFAULT,
        "fields": fields,
    }


# --------------------------------------------------------------------------
# Indeed / LinkedIn via jobspy
# --------------------------------------------------------------------------

def scrape_jobspy(category: str, term: str) -> list[JobPosting]:
    from jobspy import scrape_jobs  # import ici pour isoler les erreurs d'import

    postings: list[JobPosting] = []
    try:
        df = scrape_jobs(
            site_name=JOBSPY_SITES,
            search_term=term,
            location=LOCATION,
            results_wanted=RESULTS_PER_SEARCH,
            hours_old=HOURS_OLD,
            country_indeed="France",
            linkedin_fetch_description=False,
        )
    except Exception as e:
        print(f"[ERROR] jobspy a échoué pour '{term}' ({category}): {e}")
        record_failure("jobspy")
        return postings

    if df is None or len(df) == 0:
        return postings

    for _, row in df.iterrows():
        job_url = str(row.get("job_url") or "")
        if not job_url:
            continue
        postings.append(
            JobPosting(
                source=str(row.get("site", "")).capitalize() or "Indeed/LinkedIn",
                category=category,
                job_id=job_url,
                title=str(row.get("title", "")),
                company=str(row.get("company") or ""),
                location=str(row.get("location") or ""),
                date_posted=str(row.get("date_posted") or ""),
                url=job_url,
            )
        )
    return postings


# --------------------------------------------------------------------------
# HelloWork — scraping léger (pas supporté par jobspy)
# --------------------------------------------------------------------------

def scrape_hellowork(category: str, term: str) -> list[JobPosting]:
    """Scrape la page de résultats publique HelloWork.

    Stratégie robuste : on cherche d'abord les blocs JSON-LD
    (schema.org/JobPosting) que la plupart des sites d'emploi exposent pour
    le référencement Google for Jobs — c'est plus stable dans le temps que
    de dépendre des classes CSS de la page, qui changent à chaque refonte.
    Si aucun JSON-LD n'est trouvé, on retente avec un parsing HTML basique
    en repli.
    """
    from bs4 import BeautifulSoup

    postings: list[JobPosting] = []
    params = {"k": term, "l": HELLOWORK_LOCATION}
    try:
        r = requests.get(
            "https://www.hellowork.com/fr-fr/emploi/recherche.html",
            params=params,
            headers=HEADERS,
            timeout=20,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] HelloWork injoignable pour '{term}' ({category}): {e}")
        record_failure("hellowork")
        return postings

    soup = BeautifulSoup(r.text, "html.parser")

    # --- Stratégie 1 : JSON-LD structuré (schema.org JobPosting) ---
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "JobPosting":
                continue
            url = item.get("url") or item.get("@id") or ""
            if not url:
                continue
            org = item.get("hiringOrganization", {})
            company = org.get("name", "") if isinstance(org, dict) else ""
            postings.append(
                JobPosting(
                    source="HelloWork",
                    category=category,
                    job_id=url,
                    title=item.get("title", ""),
                    company=company,
                    location=HELLOWORK_LOCATION,
                    date_posted=item.get("datePosted", ""),
                    url=url,
                )
            )

    if postings:
        return postings

    # --- Stratégie 2 (repli) : parsing HTML basique via des liens d'offres ---
    for link in soup.select("a[href*='/emploi/'], a[href*='/emplois/']"):
        href = link.get("href", "")
        if not href or "recherche.html" in href:
            continue
        title = link.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        full_url = href if href.startswith("http") else f"https://www.hellowork.com{href}"
        postings.append(
            JobPosting(
                source="HelloWork",
                category=category,
                job_id=full_url,
                title=title,
                location=HELLOWORK_LOCATION,
                url=full_url,
            )
        )

    if not postings:
        print(
            f"[WARN] HelloWork: 0 offre extraite pour '{term}' ({category}) — "
            "la structure de la page a peut-être changé, un ajustement du "
            "scraper sera probablement nécessaire."
        )
        record_failure("hellowork")

    return postings[:RESULTS_PER_SEARCH]


# --------------------------------------------------------------------------
# Suivi des échecs répétés (pour prévenir plutôt que de tomber en silence)
# --------------------------------------------------------------------------

def record_failure(source: str) -> None:
    counts = load_json(FAILURE_LOG_FILE, {})
    counts[source] = counts.get(source, 0) + 1
    save_json(FAILURE_LOG_FILE, counts)


def reset_failure(source: str) -> None:
    counts = load_json(FAILURE_LOG_FILE, {})
    if counts.get(source):
        counts[source] = 0
        save_json(FAILURE_LOG_FILE, counts)


def warn_if_repeated_failures(threshold: int = 6) -> None:
    """6 échecs consécutifs ≈ 1h30 à raison d'un run toutes les 15 min."""
    counts = load_json(FAILURE_LOG_FILE, {})
    for source, n in counts.items():
        if n == threshold:  # averti une seule fois, pas à chaque run suivant
            send_discord_text(
                f"⚠️ Le bot n'arrive plus à récupérer d'offres depuis **{source}** "
                f"depuis {threshold} tentatives consécutives. Le site a probablement changé "
                "de structure — un ajustement du script sera nécessaire."
            )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def collect_all_jobs() -> list[JobPosting]:
    all_jobs: list[JobPosting] = []
    for category, terms in CATEGORIES.items():
        for term in terms:
            jobspy_results = scrape_jobspy(category, term)
            if jobspy_results:
                reset_failure("jobspy")
            all_jobs.extend(jobspy_results)

            hellowork_results = scrape_hellowork(category, term)
            if hellowork_results:
                reset_failure("hellowork")
            all_jobs.extend(hellowork_results)

            time.sleep(2)  # petite pause polie entre les requêtes
    return all_jobs


def dedupe_by_id(jobs: Iterable[JobPosting]) -> list[JobPosting]:
    seen_ids: set[str] = set()
    unique: list[JobPosting] = []
    for job in jobs:
        if job.job_id in seen_ids:
            continue
        seen_ids.add(job.job_id)
        unique.append(job)
    return unique


def main() -> int:
    print(f"[INFO] Démarrage — {len(CATEGORIES)} familles de recherche.")
    jobs = dedupe_by_id(collect_all_jobs())
    print(f"[INFO] {len(jobs)} offres trouvées au total sur ce run.")

    seen = load_json(SEEN_FILE, None)
    is_bootstrap = seen is None
    if is_bootstrap:
        seen = {}

    new_jobs = [job for job in jobs if job.job_id not in seen]
    # Priorité (domaine culturel) en tête du message.
    new_jobs.sort(key=lambda j: (not j.is_priority,))

    if is_bootstrap:
        print(f"[INFO] Premier lancement — {len(jobs)} offres enregistrées sans notification.")
        send_discord_text(
            f"✅ Bot d'alerte emploi initialisé.\n"
            f"{len(jobs)} offres actuelles enregistrées comme référence.\n"
            f"Tu seras notifiée dès qu'une nouvelle offre correspondant à tes critères apparaît "
            f"(⭐ = domaine culturel, ta priorité)."
        )
    else:
        print(f"[INFO] {len(new_jobs)} nouvelle(s) offre(s) depuis le dernier run.")
        if new_jobs:
            send_discord_embeds([build_embed(job) for job in new_jobs])

    for job in jobs:
        seen[job.job_id] = {
            "title": job.title,
            "category": job.category,
            "source": job.source,
            "first_seen": seen.get(job.job_id, {}).get("first_seen", time.strftime("%Y-%m-%d")),
        }

    # Purge légère : on ne garde que les 5000 entrées les plus récentes pour
    # éviter que le fichier ne grossisse indéfiniment.
    if len(seen) > 5000:
        keep_ids = {job.job_id for job in jobs}
        for old_id in list(seen.keys()):
            if len(seen) <= 5000:
                break
            if old_id not in keep_ids:
                del seen[old_id]

    save_json(SEEN_FILE, seen)
    warn_if_repeated_failures()
    return 0


if __name__ == "__main__":
    sys.exit(main())
