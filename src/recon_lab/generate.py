"""Synthetic two-org Salesforce Account exports with known ground truth.

Every organisation name is a coined word plus a sector word; every website is
on the reserved ``.example`` TLD (RFC 2606); every phone number is in the
fictional 555-0100..0199 range. Nothing here is derived from real records.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from faker import Faker

from .sfid import SalesforceId

COLUMNS = [
    "Id", "Name", "Website", "Phone", "BillingStreet", "BillingCity", "BillingState",
    "BillingPostalCode", "BillingCountry", "Industry", "NumberOfEmployees", "OwnerId",
    "ParentId", "CreatedDate", "LastModifiedDate", "Legacy_Account_Number__c",
]
TRUTH_COLUMNS = ["org", "Id", "entity_id", "role", "families"]
ORGS = ("org_a", "org_b")
INDUSTRIES = [
    "Agriculture", "Apparel", "Banking", "Biotechnology", "Chemicals", "Communications", "Construction",
    "Consulting", "Education", "Electronics", "Energy", "Engineering", "Entertainment", "Environmental",
    "Finance", "Food & Beverage", "Government", "Healthcare", "Hospitality", "Insurance", "Machinery",
    "Manufacturing", "Media", "Not For Profit", "Recreation", "Retail", "Shipping", "Technology",
    "Telecommunications", "Transportation", "Utilities", "Other",
]
SECTORS = [
    "Systems", "Technologies", "Logistics", "Partners", "International", "Manufacturing", "Services",
    "Holdings", "Solutions", "Industries", "Analytics", "Labs", "Foods", "Health", "Energy", "Supply",
    "Works", "Robotics", "Freight", "Capital", "Materials", "Networks", "Outfitters", "Dynamics",
]
SECTOR_VARIANTS = {
    "Systems": "Sys", "Technologies": "Tech", "Logistics": "Logistics Group", "Partners": "Partners Group",
    "International": "Intl", "Manufacturing": "Mfg", "Services": "Svcs", "Holdings": "Hldgs",
    "Solutions": "Sol", "Industries": "Inds", "Analytics": "Analytics Group", "Labs": "Laboratories",
    "Foods": "Food Co", "Health": "Healthcare", "Energy": "Power", "Supply": "Supply Co",
    "Works": "Workshop", "Robotics": "Robotic", "Freight": "Freightways", "Capital": "Cap",
    "Materials": "Matls", "Networks": "Net", "Outfitters": "Outfitter", "Dynamics": "Dyn",
}
LEGAL_SUFFIXES = ["Inc.", "Inc", "LLC", "Corp.", "Corporation", "Co.", "Ltd.", ""]
STREET_ABBREV = {"Street": "St", "Avenue": "Ave", "Road": "Rd", "Drive": "Dr", "Boulevard": "Blvd",
                 "Lane": "Ln", "Court": "Ct", "Suite": "Ste", "Place": "Pl", "Parkway": "Pkwy"}
STREET_SUFFIXES = ["Street", "Avenue", "Road", "Drive", "Boulevard", "Lane", "Court", "Place", "Parkway", "Way"]
AREA_CODES = ["212", "312", "415", "503", "617", "702", "720", "813", "904", "972", "206", "305", "404", "512"]
ONSETS = ["b", "br", "c", "cl", "d", "dr", "f", "fl", "g", "gr", "h", "k", "l", "m", "n", "p", "pr",
          "qu", "r", "s", "st", "t", "tr", "v", "w", "z", "sk", "j"]
VOWELS = ["a", "e", "i", "o", "u", "ae", "io", "ou", "ai", "y"]
CODAS = ["", "n", "r", "l", "x", "s", "th", "nd", "rk", "m", "ve", "q"]

DEV_FAMILIES = [
    "name_suffix", "name_case", "name_typo", "name_sector_variant",
    "web_missing", "web_subdomain", "phone_missing", "phone_changed",
    "addr_abbrev", "addr_missing", "zip_plus4", "addr_moved",
    "conflict_industry", "conflict_employees",
]
UNSEEN_FAMILIES = ["rebrand_domain"]
# Fixed window so output does not depend on the wall clock.
DATE_START = datetime(2017, 1, 1)
DATE_CREATED_END = datetime(2025, 6, 30)
DATE_END = datetime(2026, 8, 31)


@dataclass
class Entity:
    entity_id: str
    coined: str
    sector: str
    suffix: str
    slug: str
    street: str
    city: str
    state: str
    postcode: str
    industry: str
    employees: int
    phone_area: str
    phone_line: int
    twin_of: str | None = None

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.coined, self.sector, self.suffix) if p)


@dataclass
class GeneratorConfig:
    seed: int
    rows_per_org: int = 1000
    shared: int = 250
    dups_per_org: int = 40
    twins: int = 75
    unseen_rate: float = 0.0  # share of shared org_b copies given an unseen family


@dataclass
class Dataset:
    rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    truth: list[dict[str, str]] = field(default_factory=list)


class NameForge:
    @staticmethod
    def coined(rng: random.Random) -> str:
        word = rng.choice(ONSETS) + rng.choice(VOWELS) + rng.choice(ONSETS) + rng.choice(VOWELS) + rng.choice(CODAS)
        return word[:1].upper() + word[1:]


class CorruptionModel:
    """Applies named corruption families to a rendered Account row."""

    # Per-family probabilities for a cross-org copy of the same company. Real
    # multi-CRM exports are sparse and disagree a lot, so these are deliberately
    # heavy; a copy always gets at least one family.
    COPY_PROFILE = {
        "name_suffix": 0.35, "name_case": 0.15, "name_typo": 0.2, "name_sector_variant": 0.3,
        "web_missing": 0.35, "web_subdomain": 0.15, "phone_missing": 0.3, "phone_changed": 0.25,
        "addr_abbrev": 0.4, "addr_missing": 0.15, "zip_plus4": 0.2, "addr_moved": 0.3,
        "conflict_industry": 0.3, "conflict_employees": 0.3,
    }
    # Light noise on an org's own (primary) record.
    PRIMARY_PROFILE = {"web_missing": 0.12, "phone_missing": 0.08, "addr_abbrev": 0.15, "zip_plus4": 0.08}

    @staticmethod
    def pick(rng: random.Random, profile: dict[str, float], at_least_one: bool) -> list[str]:
        families = [name for name in DEV_FAMILIES if name in profile and rng.random() < profile[name]]
        if at_least_one and not families:
            families = [rng.choice(sorted(profile))]
        if "web_missing" in families and "web_subdomain" in families:
            families.remove("web_subdomain")
        if "phone_missing" in families and "phone_changed" in families:
            families.remove("phone_changed")
        return sorted(families)

    @staticmethod
    def typo(word: str, rng: random.Random) -> str:
        if len(word) < 4:
            return word + word[-1]
        i = rng.randrange(1, len(word) - 1)
        op = rng.choice(["swap", "drop", "double"])
        if op == "swap":
            return word[:i] + word[i + 1] + word[i] + word[i + 2:]
        if op == "drop":
            return word[:i] + word[i + 1:]
        return word[:i] + word[i] + word[i:]

    @staticmethod
    def apply(row: dict[str, str], entity: Entity, families: list[str], rng: random.Random, fake: Faker) -> None:
        coined, sector, suffix = entity.coined, entity.sector, entity.suffix
        for family in families:
            if family == "name_suffix":
                suffix = rng.choice([s for s in LEGAL_SUFFIXES if s != entity.suffix])
            elif family == "name_typo":
                coined = CorruptionModel.typo(coined, rng)
            elif family == "name_sector_variant":
                sector = SECTOR_VARIANTS[entity.sector]
        row["Name"] = " ".join(p for p in (coined, sector, suffix) if p)
        if "name_case" in families:
            row["Name"] = rng.choice([str.upper, str.lower])(row["Name"])
        if "rebrand_domain" in families:
            row["Website"] = CorruptionModel.web_format(f"{NameForge.coined(rng).lower()}{rng.randint(2, 99)}.example", rng)
        elif "web_subdomain" in families:
            row["Website"] = CorruptionModel.web_format(f"{rng.choice(['us', 'shop', 'app', 'go'])}.{entity.slug}.example", rng)
        if "web_missing" in families:
            row["Website"] = ""
        if "phone_changed" in families:
            row["Phone"] = CorruptionModel.phone_format(entity.phone_area, (entity.phone_line + rng.randint(1, 99)) % 100, rng)
        if "phone_missing" in families:
            row["Phone"] = ""
        if "addr_moved" in families:
            row["BillingStreet"] = f"{fake.building_number()} {fake.last_name()} {rng.choice(STREET_SUFFIXES)}"
            row["BillingPostalCode"] = fake.zipcode_in_state(entity.state)
        if "addr_abbrev" in families:
            row["BillingStreet"] = " ".join(STREET_ABBREV.get(t, t) for t in row["BillingStreet"].split(" "))
        if "zip_plus4" in families and row["BillingPostalCode"]:
            row["BillingPostalCode"] = f"{row['BillingPostalCode'][:5]}-{rng.randint(1000, 9999)}"
        if "addr_missing" in families:
            row["BillingStreet"] = ""
        if "conflict_industry" in families:
            row["Industry"] = rng.choice([i for i in INDUSTRIES if i != entity.industry])
        if "conflict_employees" in families:
            row["NumberOfEmployees"] = rng.choice(["", str(max(1, int(entity.employees * rng.uniform(0.4, 2.5))))])

    @staticmethod
    def web_format(host: str, rng: random.Random) -> str:
        return rng.choice([f"https://www.{host}", f"http://{host}", f"www.{host}", host, f"https://{host}/",
                           f"https://www.{host}/about", f"WWW.{host.upper()}"])

    @staticmethod
    def phone_format(area: str, line: int, rng: random.Random) -> str:
        last = f"01{line:02d}"
        return rng.choice([f"({area}) 555-{last}", f"{area}-555-{last}", f"{area}.555.{last}",
                           f"+1 {area} 555 {last}", f"1-{area}-555-{last}"])


class EntityFactory:
    @staticmethod
    def build(config: GeneratorConfig, rng: random.Random, fake: Faker) -> list[Entity]:
        unique_per_org = config.rows_per_org - config.dups_per_org
        total = 2 * unique_per_org - config.shared
        coined_seen: set[str] = set()
        slugs: set[str] = set()
        entities: list[Entity] = []
        base_count = total - config.twins
        while len(entities) < base_count:
            coined = NameForge.coined(rng)
            if coined in coined_seen:
                continue
            coined_seen.add(coined)
            entities.append(EntityFactory._entity(len(entities), coined, rng.choice(SECTORS), coined.lower(), None, rng, fake))
            slugs.add(coined.lower())
        for twin_source in rng.sample(entities, config.twins):
            sector = rng.choice([s for s in SECTORS if s != twin_source.sector])
            slug = f"{twin_source.coined}{sector}".lower()
            twin = EntityFactory._entity(len(entities), twin_source.coined, sector, slug, twin_source.entity_id, rng, fake)
            roll = rng.random()
            if roll < 0.5:  # same metro area / ZIP: makes the negative hard
                twin.city, twin.state, twin.postcode = twin_source.city, twin_source.state, twin_source.postcode
            if roll < 0.2:  # sister company in the same building, sharing the switchboard area code
                twin.street, twin.phone_area = twin_source.street, twin_source.phone_area
            entities.append(twin)
        return entities

    @staticmethod
    def _entity(index: int, coined: str, sector: str, slug: str, twin_of: str | None,
                rng: random.Random, fake: Faker) -> Entity:
        state = fake.state_abbr(include_territories=False)
        street = f"{fake.building_number()} {fake.last_name()} {rng.choice(STREET_SUFFIXES)}"
        if rng.random() < 0.25:
            street += f" Suite {rng.randint(100, 999)}"
        return Entity(
            entity_id=f"E{index:05d}", coined=coined, sector=sector, suffix=rng.choice(LEGAL_SUFFIXES), slug=slug,
            street=street, city=fake.city(), state=state, postcode=fake.zipcode_in_state(state),
            industry=rng.choice(INDUSTRIES), employees=rng.choice([5, 12, 25, 40, 80, 150, 300, 750, 1200, 5000]) + rng.randint(0, 9),
            phone_area=rng.choice(AREA_CODES), phone_line=rng.randint(0, 99), twin_of=twin_of,
        )


class OrgExporter:
    """Renders entities as org-specific Salesforce Account export rows."""

    PODS = {"org_a": "5A", "org_b": "8g"}
    COUNTRY = {"org_a": ["United States"], "org_b": ["US", "USA", "United States of America"]}

    @staticmethod
    def render(entity: Entity, org: str, families: list[str], rng: random.Random, fake: Faker,
               owners: list[str]) -> dict[str, str]:
        created = fake.date_time_between(start_date=DATE_START, end_date=DATE_CREATED_END)
        modified = fake.date_time_between(start_date=created, end_date=DATE_END)
        row = {
            "Id": SalesforceId.make("001", OrgExporter.PODS[org], rng),
            "Name": entity.name,
            "Website": CorruptionModel.web_format(f"{entity.slug}.example", rng),
            "Phone": CorruptionModel.phone_format(entity.phone_area, entity.phone_line, rng),
            "BillingStreet": entity.street,
            "BillingCity": entity.city,
            "BillingState": entity.state,
            "BillingPostalCode": entity.postcode,
            "BillingCountry": rng.choice(OrgExporter.COUNTRY[org]),
            "Industry": entity.industry,
            "NumberOfEmployees": str(entity.employees),
            "OwnerId": rng.choice(owners),
            "ParentId": "",
            "CreatedDate": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "LastModifiedDate": modified.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "Legacy_Account_Number__c": f"{'LA' if org == 'org_a' else 'BX'}-{rng.randint(100000, 999999)}",
        }
        CorruptionModel.apply(row, entity, families, rng, fake)
        return row


class DatasetGenerator:
    @staticmethod
    def generate(config: GeneratorConfig) -> Dataset:
        rng = random.Random(config.seed)
        fake = Faker("en_US")
        fake.seed_instance(config.seed)
        entities = EntityFactory.build(config, rng, fake)
        unique_per_org = config.rows_per_org - config.dups_per_org
        order = entities[:]
        rng.shuffle(order)
        shared = order[: config.shared]
        only_a = order[config.shared: unique_per_org]
        only_b = order[unique_per_org:]
        assert len(only_b) == unique_per_org - config.shared
        members = {"org_a": shared + only_a, "org_b": shared + only_b}
        shared_ids = {e.entity_id for e in shared}
        dataset = Dataset(rows={org: [] for org in ORGS})
        for org in ORGS:
            owners = [SalesforceId.make("005", OrgExporter.PODS[org], rng) for _ in range(6)]
            for entity in members[org]:
                if org == "org_b" and entity.entity_id in shared_ids:
                    role, families = "shared_copy", CorruptionModel.pick(rng, CorruptionModel.COPY_PROFILE, True)
                    if config.unseen_rate and rng.random() < config.unseen_rate:
                        families = sorted(set(families) - {"web_missing", "web_subdomain"} | {"rebrand_domain"})
                else:
                    role = "primary"
                    families = CorruptionModel.pick(rng, CorruptionModel.PRIMARY_PROFILE, False)
                DatasetGenerator._add(dataset, org, entity, role, families, rng, fake, owners)
            for entity in rng.sample(members[org], config.dups_per_org):
                DatasetGenerator._add(dataset, org, entity, "intra_dup",
                                      CorruptionModel.pick(rng, CorruptionModel.COPY_PROFILE, True), rng, fake, owners)
            rng.shuffle(dataset.rows[org])
        dataset.truth.sort(key=lambda t: (t["org"], t["entity_id"], t["role"], t["Id"]))
        return dataset

    @staticmethod
    def _add(dataset: Dataset, org: str, entity: Entity, role: str, families: list[str],
             rng: random.Random, fake: Faker, owners: list[str]) -> None:
        row = OrgExporter.render(entity, org, families, rng, fake, owners)
        dataset.rows[org].append(row)
        dataset.truth.append({"org": org, "Id": row["Id"], "entity_id": entity.entity_id, "role": role,
                              "families": "|".join(families)})

    @staticmethod
    def write(dataset: Dataset, root: Path, seed: int) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for org in ORGS:
            path = root / "synthetic" / str(seed) / org / "Account.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
                writer.writeheader()
                writer.writerows(dataset.rows[org])
            paths[org] = path
        truth = root / "ground_truth" / str(seed) / "entity_map.csv"
        truth.parent.mkdir(parents=True, exist_ok=True)
        with truth.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRUTH_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(dataset.truth)
        paths["truth"] = truth
        return paths


class AccountCsv:
    @staticmethod
    def read(path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def load_seed(root: Path, seed: int) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
        rows = {org: AccountCsv.read(root / "synthetic" / str(seed) / org / "Account.csv") for org in ORGS}
        truth = AccountCsv.read(root / "ground_truth" / str(seed) / "entity_map.csv")
        return rows, truth
