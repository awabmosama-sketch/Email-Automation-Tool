import csv
import json

from . import config, database


CONTACT_FIELDS = (
    "name",
    "email",
    "phone",
    "company_name",
    "target_role",
    "target_industry",
    "details",
)


def migrate() -> None:
    if not database.is_configured():
        raise RuntimeError("Set DATABASE_URL before running the Neon migration.")

    contacts = []
    if config.CONTACTS_FILE.exists():
        with config.CONTACTS_FILE.open(newline="", encoding="utf-8") as contacts_file:
            for row in csv.DictReader(contacts_file):
                contact = {field: (row.get(field) or "").strip() for field in CONTACT_FIELDS}
                if not contact["details"]:
                    contact["details"] = (row.get("context") or "").strip()
                if contact["email"] and contact["name"]:
                    contacts.append(contact)
    database.seed_contacts(contacts)

    if config.COMPANIES_FILE.exists():
        with config.COMPANIES_FILE.open(encoding="utf-8") as companies_file:
            profiles = json.load(companies_file)
        if isinstance(profiles, dict):
            database.save_company_profiles(profiles)

    print(f"Migrated {len(contacts)} contacts and company profiles to Neon.")


if __name__ == "__main__":
    migrate()
