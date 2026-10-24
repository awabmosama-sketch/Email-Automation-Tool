import json
import logging
from pathlib import Path
from . import config
from . import database

logger = logging.getLogger(__name__)

DEFAULT_COMPANY_PROFILES = {
    "F-Tune": {
        "services": [
            "Custom software development",
            "Mobile app development",
            "UI/UX design",
        ],
        "value_proposition": (
            "We help businesses digitize operations with tailored software solutions."
        ),
        "tone": "friendly-professional",
        "signature": "Best regards,\nAwab Al Barmawy\nF-Tune Team\nawab@f-tune.com",
    },
    "CloudPulse": {
        "services": [
            "Cloud migration & DevOps",
            "AWS & Azure architecture",
            "24/7 infrastructure monitoring",
        ],
        "value_proposition": (
            "We scale infrastructure reliably and cut cloud hosting overhead."
        ),
        "tone": "professional-technical",
        "signature": "Sincerely,\nCloudPulse Engineering\ncontact@cloudpulse.io",
    },
    "GrowthWave": {
        "services": [
            "B2B inbound lead generation",
            "Performance marketing & SEO",
            "Conversion rate optimization",
        ],
        "value_proposition": (
            "We accelerate qualified pipeline growth for high-growth tech companies."
        ),
        "tone": "energetic-consultative",
        "signature": "Cheers,\nGrowthWave Partnerships\nhello@growthwave.agency",
    },
}

GENERIC_PROFILE = {
    "services": [
        "Strategic consulting",
        "Process automation",
        "Custom technical solutions",
    ],
    "value_proposition": (
        "We deliver specialized solutions that help modern businesses scale efficiently."
    ),
    "tone": "formal-professional",
    "signature": "Best regards,\nBusiness Solutions Team\ncontact@solutions.example.com",
}


def load_company_profiles() -> dict:
    """Loads company profiles from Neon or the optional local fallback."""
    if database.is_configured():
        profiles = database.load_company_profiles()
        seeded = database.get_meta("companies_seeded") is not None
        if profiles:
            if not seeded:
                database.set_meta("companies_seeded", "1")
            return profiles
        # Seed the starter profiles only on a brand-new database. Once the
        # marker exists, an empty table means the user deleted every company.
        if not seeded:
            save_all_company_profiles(DEFAULT_COMPANY_PROFILES)
            database.set_meta("companies_seeded", "1")
            return dict(DEFAULT_COMPANY_PROFILES)
        return {}

    companies_file = config.COMPANIES_FILE
    if not companies_file.exists():
        save_all_company_profiles(DEFAULT_COMPANY_PROFILES)
        return dict(DEFAULT_COMPANY_PROFILES)

    try:
        with open(companies_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as err:
        logger.warning("Could not read %s, using defaults: %s", companies_file, err)

    return dict(DEFAULT_COMPANY_PROFILES)


def save_all_company_profiles(profiles: dict) -> None:
    """Persists all company profiles to the local fallback file."""
    if database.is_configured():
        database.save_company_profiles(profiles)
        return

    companies_file = config.COMPANIES_FILE
    companies_file.parent.mkdir(parents=True, exist_ok=True)
    with open(companies_file, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


def add_or_update_company_profile(
    name: str,
    services: list[str],
    value_proposition: str,
    tone: str,
    signature: str,
) -> dict:
    """Adds a new company profile and saves it to companies storage."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Company name cannot be blank.")

    clean_services = [s.strip() for s in services if isinstance(s, str) and s.strip()]
    if not clean_services:
        raise ValueError("At least one service must be specified.")

    profiles = load_company_profiles()
    profile = {
        "services": clean_services,
        "value_proposition": value_proposition.strip(),
        "tone": tone.strip() or "friendly-professional",
        "signature": signature.strip(),
    }
    profiles[clean_name] = profile
    save_all_company_profiles(profiles)
    return profile


def get_company_profile(company_name: str) -> dict:
    """Returns the company profile or a fallback generic profile if not found."""
    if not company_name:
        logger.warning("Empty company name provided; falling back to generic profile.")
        return GENERIC_PROFILE

    profiles = load_company_profiles()
    profile = profiles.get(company_name)
    if not profile:
        logger.warning(
            "Company '%s' not found in profiles; falling back to generic profile.",
            company_name,
        )
        return GENERIC_PROFILE
    return profile


class _CompanyProfilesProxy(dict):
    """Proxy dictionary so code referencing companies.COMPANY_PROFILES stays in sync with file."""

    def __getitem__(self, key):
        return load_company_profiles()[key]

    def get(self, key, default=None):
        return load_company_profiles().get(key, default)

    def keys(self):
        return load_company_profiles().keys()

    def values(self):
        return load_company_profiles().values()

    def items(self):
        return load_company_profiles().items()

    def __iter__(self):
        return iter(load_company_profiles())

    def __len__(self):
        return len(load_company_profiles())

    def __contains__(self, key):
        return key in load_company_profiles()


COMPANY_PROFILES = _CompanyProfilesProxy()
