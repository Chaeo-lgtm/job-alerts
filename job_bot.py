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
import re
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

warnings.filterwarnings("ignore")

# --------------------------------------------------------------------------
# Configuration — à ajuster librement sans toucher au reste du script
# --------------------------------------------------------------------------

LOCATION = "Paris, France"          # utilisé pour Indeed / LinkedIn (jobspy)
HELLOWORK_LOCATION = "Paris"        # utilisé pour l'URL de recherche HelloWork
DISTANCE_MILES = 15                 # rayon de recherche jobspy (~24 km, couvre large autour de Paris)
MAX_AGE_DAYS = 7                    # n'afficher que les offres postées il y a moins d'une semaine
HOURS_OLD = MAX_AGE_DAYS * 24        # équivalent en heures, transmis à jobspy (Indeed/LinkedIn)
RESULTS_PER_SEARCH = 20             # nb de résultats à examiner par recherche

# Ne garder que les offres dans ces départements (75 = Paris, 92 = Hauts-de-
# Seine, 94 = Val-de-Marne). Le rayon de recherche ci-dessus remonte aussi
# des offres d'autres départements limitrophes (93, 77, 78, 91, 95) : elles
# sont filtrées après coup grâce à TARGET_DEPARTMENTS / EXCLUDED_DEPARTMENTS.
TARGET_DEPARTMENTS = {"75", "92", "94"}
EXCLUDED_DEPARTMENTS = {"93", "77", "78", "91", "95"}

# Titres contenant un de ces mots = offre écartée (stages et alternances).
EXCLUDED_CONTRACT_KEYWORDS = [
    "stage", "stagiaire",
    "alternance", "alternant", "alternante",
    "apprenti", "apprentie", "apprentissage",
]

# Restauration : on écarte les postes de cuisine (chef de partie, commis,
# plonge...) pour ne garder que le service en salle et le comptoir/barista —
# SAUF si l'offre est vraiment à côté de chez elle (Paris intra-muros, 75) :
# dans ce cas précis, autant la voir plutôt que la perdre. Loin de Paris, ça
# ne vaut pas le coup, donc c'est écarté (voir is_excluded_kitchen_role).
EXCLUDED_KITCHEN_KEYWORDS = [
    "cuisine", "cuisinier", "cuisinière",
    "chef de partie", "chef de cuisine", "chef cuisinier",
    "commis de cuisine", "commis cuisine", "second de cuisine",
    "sous-chef", "sous chef", "aide-cuisinier", "aide cuisinier",
    "plongeur", "plongeuse", "plonge",
    "patissier", "pâtissier", "patissiere", "pâtissière",
    "boulanger", "boulangere", "boulangère",
]
KITCHEN_EXCEPTION_DEPARTMENT = "75"  # Paris intra-muros — "à côté de chez moi"

# "Dessinateur" est aussi un métier technique du BTP/industrie (dessinateur
# projeteur, dessinateur BTP, bureau d'études...) qui n'a rien à voir avec
# l'illustration/BD — ça remonte parfois sur la recherche "dessinateur" et
# doit être écarté.
EXCLUDED_TECHNICAL_DRAWING_KEYWORDS = [
    "projeteur", "dessinateur btp", "dessinateur bâtiment", "dessinateur batiment",
    "dessinateur industriel", "dessinateur mécanique", "dessinateur mecanique",
    "dessinateur électrique", "dessinateur electrique", "dessinateur structure",
    "bureau d'études", "bureau d etudes", "génie civil", "genie civil",
    "vrd", "cvc", "topographe", "géomètre", "geometre",
]

# Postes trop qualifiés/expérimentés : écartés PARTOUT, y compris dans le
# domaine culturel (ex. "Responsable Architecture Communication", "Chef de
# Service - Musée du Louvre" ne sont pas plus pertinents que "Responsable de
# magasin").
EXCLUDED_SENIOR_KEYWORDS = [
    "responsable", "chargé de projet", "chargée de projet",
    "chef de service", "chef d'équipe", "cheffe d'équipe",
    "directeur", "directrice", "superviseur", "superviseuse",
]
# "manager" reste toléré UNIQUEMENT dans le domaine culturel/numérique, car
# "community manager" est un intitulé recherché — mais pas ailleurs
# ("manager restauration", "store manager"...).
EXCLUDED_SENIOR_KEYWORDS_SAFETY_NET_ONLY = ["manager"]

# Chaque "famille" = un ou plusieurs termes de recherche regroupés sous un
# même libellé. Ajoute / retire des termes ou des familles ici si besoin.
# Les familles listées dans PRIORITY_CATEGORIES ressortent en priorité
# (couleur dorée + en tête) dans les notifications Discord.
CATEGORIES: dict[str, list[str]] = {
    # --- domaine culturel / créatif : profil idéal (calqué sur le CV) ---
    # Intitulé de poste réellement occupé : "Médiatrice culturelle et numérique"
    "Médiation culturelle / bibliothèque": [
        "médiateur culturel", "médiatrice culturelle", "médiation numérique",
        "bibliothécaire", "assistant de bibliothèque", "agent de bibliothèque",
        "magasinier bibliothèque",
    ],
    # Tâches CV : création de visuels/contenus réseaux sociaux, interviews, montage vidéo promo
    "Communication / création de contenu": [
        "chargé de communication", "community manager",
        "créateur de contenu", "assistant communication",
    ],
    # Tâches CV : organisation/coordination d'événements culturels, billetterie
    "Événementiel": [
        "chargé événementiel", "assistant événementiel", "chargé de billetterie",
    ],
    # Compétences CV : Photoshop/InDesign/Premiere Pro/Lightroom/After Effects/Procreate,
    # montage vidéo, photographie (Blast Fest, Corée Graphie)
    "Audiovisuel / photo / graphisme": [
        "monteur vidéo", "photographe", "graphiste", "infographiste",
    ],
    # Formation CV : prépa Illustration & Concept Art (ESMA), Licence Arts Plastiques
    "Illustration / arts visuels": [
        "illustrateur", "illustratrice", "dessinateur", "dessinatrice",
    ],
    # --- job alimentaire : filet de sécurité, calqué sur l'expérience réelle ---
    # Expérience CV : vente et management d'équipe (La Banquise)
    "Vente": ["vendeur vendeuse", "conseiller de vente"],
    # Expérience CV : accueil du public (bibliothèque ALMA)
    "Accueil / réception": ["agent d'accueil", "hôte hôtesse d'accueil", "réceptionniste"],
    # Intitulé de poste réellement occupé : "Équipière polyvalente" (Quick, La Banquise)
    "Équipier polyvalent": ["équipier polyvalent", "équipière polyvalente"],
    # Expérience CV : comptoir/salle (Quick) — service en salle, pas cuisine
    "Restauration (service)": ["serveur serveuse", "équipier restauration"],
}

PRIORITY_CATEGORIES = {
    "Médiation culturelle / bibliothèque",
    "Communication / création de contenu",
    "Événementiel",
    "Audiovisuel / photo / graphisme",
    "Illustration / arts visuels",
}

# Garde-fou anti-bruit : Indeed/LinkedIn renvoient parfois des offres
# "apparentées" qui ne contiennent même pas le terme cherché (ex. une
# recherche "médiation numérique" qui ramène un poste d'aide-soignant ou
# d'éducateur). On ne garde une offre que si son titre contient au moins un
# de ces mots-clés pour sa catégorie — sinon elle est écartée, même si le
# site l'a proposée.
CATEGORY_REQUIRED_KEYWORDS: dict[str, list[str]] = {
    "Médiation culturelle / bibliothèque": [
        "médiat", "mediat", "culture", "culturel", "patrimoine",
        "musée", "musee", "bibliothèque", "biblioth",
    ],
    "Communication / création de contenu": [
        "communication", "community", "contenu", "réseaux sociaux",
        "reseaux sociaux", "social media", "digital",
    ],
    "Événementiel": [
        "événement", "evenement", "événementiel", "evenementiel", "billetterie",
    ],
    "Audiovisuel / photo / graphisme": [
        "vidéo", "video", "photo", "graphiste", "graphisme",
        "infographie", "audiovisuel", "montage",
    ],
    "Illustration / arts visuels": [
        "illustrat", "dessin", "concept art", "bd", "bande dessinée",
    ],
    "Vente": ["vente", "vendeur", "vendeuse", "boutique", "magasin", "conseiller de vente"],
    "Accueil / réception": ["accueil", "réception", "reception"],
    "Équipier polyvalent": ["polyvalent", "polyvalente", "équipier", "équipière", "equipier", "equipiere"],
    "Restauration (service)": ["restauration", "serveur", "serveuse", "salle"],
}


def is_title_relevant(job: "JobPosting") -> bool:
    """False si le titre ne contient aucun mot-clé attendu pour sa
    catégorie — signe qu'il s'agit d'un résultat "apparenté" hors sujet
    plutôt que d'une vraie correspondance."""
    keywords = CATEGORY_REQUIRED_KEYWORDS.get(job.category)
    if not keywords:
        return True  # catégorie non répertoriée : on ne filtre pas à l'aveugle
    t = (job.title or "").lower()
    return any(kw in t for kw in keywords)

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
# Filtre d'ancienneté (offres de plus de MAX_AGE_DAYS jours écartées)
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_date_flexible(date_str: str):
    """Essaie d'extraire une date d'une chaîne (ISO 8601 le plus souvent).
    Retourne None si la date est absente ou illisible."""
    if not date_str:
        return None
    match = _DATE_RE.search(date_str)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_recent_enough(job: "JobPosting", max_days: int = MAX_AGE_DAYS) -> bool:
    """Écarte une offre HelloWork trop ancienne. Si la date est illisible ou
    absente (ça arrive avec le repli HTML sans JSON-LD), on la garde plutôt
    que de risquer de perdre une offre pertinente."""
    parsed = parse_date_flexible(job.date_posted)
    if parsed is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    return parsed >= cutoff


# --------------------------------------------------------------------------
# Filtres : contrat (pas de stage/alternance) et département (75/92/94)
# --------------------------------------------------------------------------

def is_excluded_contract(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in EXCLUDED_CONTRACT_KEYWORDS)


def is_excluded_kitchen_role(job: "JobPosting") -> bool:
    """Écarte les postes de cuisine — sauf s'ils sont vraiment à côté de chez
    elle (Paris intra-muros, 75), auquel cas ça vaut le coup de les garder
    malgré tout."""
    t = (job.title or "").lower()
    if not any(kw in t for kw in EXCLUDED_KITCHEN_KEYWORDS):
        return False
    return extract_department(job.location) != KITCHEN_EXCEPTION_DEPARTMENT


def is_excluded_technical_drawing(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in EXCLUDED_TECHNICAL_DRAWING_KEYWORDS)


def is_excluded_senior_role(job: "JobPosting") -> bool:
    """Écarte les intitulés trop qualifiés (responsable, chargé de projet,
    chef de service, directeur...) partout, y compris dans le domaine
    culturel. "manager" seul reste toléré dans le domaine culturel/numérique
    (community manager), mais pas ailleurs."""
    t = (job.title or "").lower()
    if any(kw in t for kw in EXCLUDED_SENIOR_KEYWORDS):
        return True
    if not job.is_priority and any(kw in t for kw in EXCLUDED_SENIOR_KEYWORDS_SAFETY_NET_ONLY):
        return True
    return False


# Villes connues pour désambiguïser un lieu qui n'affiche pas de code postal
# ou de département entre parenthèses. Liste non exhaustive, à compléter au
# besoin — mais ça couvre les cas les plus fréquents en Île-de-France.
_CITY_TO_DEPT = {
    "paris": "75",
    "boulogne-billancourt": "92", "nanterre": "92", "courbevoie": "92",
    "issy-les-moulineaux": "92", "levallois-perret": "92", "clichy": "92",
    "neuilly-sur-seine": "92", "rueil-malmaison": "92", "colombes": "92",
    "asnieres-sur-seine": "92", "asnières-sur-seine": "92", "puteaux": "92",
    "montrouge": "92", "chatillon": "92", "châtillon": "92",
    "gennevilliers": "92", "sceaux": "92", "antony": "92",
    "creteil": "94", "créteil": "94", "vitry-sur-seine": "94",
    "champigny-sur-marne": "94", "saint-maur-des-fosses": "94",
    "saint-maur-des-fossés": "94", "ivry-sur-seine": "94", "vincennes": "94",
    "fontenay-sous-bois": "94", "villejuif": "94", "maisons-alfort": "94",
    "charenton-le-pont": "94", "nogent-sur-marne": "94", "alfortville": "94",
    "cachan": "94", "le kremlin-bicetre": "94", "le kremlin-bicêtre": "94",
    # départements limitrophes explicitement écartés
    "saint-denis": "93", "bobigny": "93", "montreuil": "93",
    "aubervilliers": "93", "pantin": "93", "bagnolet": "93", "drancy": "93",
    "versailles": "78", "saint-germain-en-laye": "78", "sartrouville": "78",
    "evry": "91", "évry": "91", "corbeil-essonnes": "91", "massy": "91",
    "cergy": "95", "argenteuil": "95", "sarcelles": "95",
    "meaux": "77", "melun": "77", "chelles": "77",
}

_DEPT_IN_PARENS_RE = re.compile(r"\((\d{2,3})\)")
_POSTAL_CODE_RE = re.compile(r"\b(\d{5})\b")


def extract_department(location: str):
    """Tente d'identifier le département depuis un texte de lieu. Retourne
    le code (ex. '75') ou None si indéterminable."""
    if not location:
        return None
    match = _DEPT_IN_PARENS_RE.search(location)
    if match:
        code = match.group(1)
        return code[:2] if len(code) >= 2 else code
    match = _POSTAL_CODE_RE.search(location)
    if match:
        return match.group(1)[:2]
    loc_lower = location.lower()
    for city, dept in _CITY_TO_DEPT.items():
        if city in loc_lower:
            return dept
    return None


def is_in_target_area(location: str) -> bool:
    """True si le département est 75/92/94, ou si on ne peut pas le
    déterminer (mieux vaut garder une offre ambiguë que d'en perdre une
    valide à cause d'un format de lieu inhabituel)."""
    dept = extract_department(location)
    if dept is None:
        return True
    if dept in EXCLUDED_DEPARTMENTS:
        return False
    return dept in TARGET_DEPARTMENTS


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
            distance=DISTANCE_MILES,
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

    postings = [job for job in postings if is_recent_enough(job)]
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
    print(f"[INFO] {len(jobs)} offres trouvées avant filtrage.")

    jobs = [j for j in jobs if not is_excluded_contract(j.title)]
    jobs = [j for j in jobs if not is_excluded_kitchen_role(j)]
    jobs = [j for j in jobs if not is_excluded_technical_drawing(j.title)]
    jobs = [j for j in jobs if not is_excluded_senior_role(j)]
    jobs = [j for j in jobs if is_in_target_area(j.location)]
    jobs = [j for j in jobs if is_title_relevant(j)]
    print(f"[INFO] {len(jobs)} offres retenues après filtrage (contrat / cuisine / dessin technique / niveau / secteur / pertinence).")

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
