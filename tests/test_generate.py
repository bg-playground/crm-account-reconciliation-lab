import re
import unittest
from collections import Counter

from recon_lab.blocking import Blocker
from recon_lab.generate import COLUMNS, ORGS, DatasetGenerator, GeneratorConfig
from recon_lab.normalize import Normalizer
from recon_lab.sfid import SalesforceId


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev = DatasetGenerator.generate(GeneratorConfig(seed=101))
        cls.test = DatasetGenerator.generate(GeneratorConfig(seed=202, unseen_rate=0.15))

    def test_sizes_and_roles(self):
        for data in (self.dev, self.test):
            for org in ORGS:
                self.assertEqual(len(data.rows[org]), 1000)
                self.assertEqual(list(data.rows[org][0].keys()), COLUMNS)
            roles = Counter((t["org"], t["role"]) for t in data.truth)
            self.assertEqual(roles[("org_b", "shared_copy")], 250)
            self.assertEqual(roles[("org_a", "intra_dup")], 40)
            self.assertEqual(roles[("org_b", "intra_dup")], 40)

    def test_shared_entities(self):
        by_org = {org: {t["entity_id"] for t in self.dev.truth if t["org"] == org} for org in ORGS}
        self.assertEqual(len(by_org["org_a"] & by_org["org_b"]), 250)

    def test_reserved_domains_and_phones(self):
        for org in ORGS:
            for row in self.dev.rows[org] + self.test.rows[org]:
                if row["Website"]:
                    self.assertTrue(Normalizer.domain(row["Website"]).endswith(".example"), row["Website"])
                if row["Phone"]:
                    self.assertRegex(re.sub(r"\D", "", row["Phone"])[-7:], r"^55501\d\d$")

    def test_ids_valid_and_disjoint(self):
        ids = {org: [r["Id"] for r in self.dev.rows[org]] for org in ORGS}
        for org in ORGS:
            self.assertEqual(len(set(ids[org])), 1000)
            self.assertTrue(all(SalesforceId.is_valid18(i) and i.startswith("001") for i in ids[org]))
            self.assertTrue(all(SalesforceId.is_valid18(r["OwnerId"]) and r["OwnerId"].startswith("005")
                                for r in self.dev.rows[org]))
        self.assertFalse(set(ids["org_a"]) & set(ids["org_b"]))

    def test_dates_format(self):
        row = self.dev.rows["org_a"][0]
        self.assertRegex(row["CreatedDate"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z$")
        self.assertLessEqual(row["CreatedDate"], row["LastModifiedDate"])

    def test_deterministic(self):
        again = DatasetGenerator.generate(GeneratorConfig(seed=101))
        self.assertEqual(again.rows, self.dev.rows)
        self.assertEqual(again.truth, self.dev.truth)

    def test_unseen_family_only_in_test(self):
        dev_fams = {f for t in self.dev.truth for f in t["families"].split("|") if f}
        test_fams = Counter(f for t in self.test.truth for f in t["families"].split("|") if f)
        self.assertNotIn("rebrand_domain", dev_fams)
        self.assertGreater(test_fams["rebrand_domain"], 20)

    def test_true_pairs(self):
        pairs = Blocker.true_pairs(self.dev.truth)
        self.assertGreaterEqual(len(pairs), 250 + 80)

    def test_leak_rows_shuffled(self):
        # Row order must not follow entity order or role.
        truth = {(t["org"], t["Id"]): t for t in self.dev.truth}
        entities = [truth[("org_a", r["Id"])]["entity_id"] for r in self.dev.rows["org_a"]]
        self.assertNotEqual(entities, sorted(entities))
        roles = [truth[("org_a", r["Id"])]["role"] for r in self.dev.rows["org_a"]]
        self.assertIn("intra_dup", roles[:500])

    def test_leak_dates_and_legacy_numbers_not_copied_across_orgs(self):
        truth = {(t["org"], t["Id"]): t["entity_id"] for t in self.dev.truth}
        by_entity = {}
        for org in ORGS:
            for row in self.dev.rows[org]:
                by_entity.setdefault(truth[(org, row["Id"])], {}).setdefault(org, []).append(row)
        shared = [v for v in by_entity.values() if len(v) == 2]
        self.assertGreaterEqual(len(shared), 250)
        same_created = sum(1 for v in shared if v["org_a"][0]["CreatedDate"] == v["org_b"][0]["CreatedDate"])
        same_legacy = sum(1 for v in shared if v["org_a"][0]["Legacy_Account_Number__c"] == v["org_b"][0]["Legacy_Account_Number__c"])
        self.assertEqual(same_created, 0)
        self.assertEqual(same_legacy, 0)

    def test_leak_no_shared_id_sequence(self):
        a = sorted(r["Id"][6:15] for r in self.dev.rows["org_a"])
        b = sorted(r["Id"][6:15] for r in self.dev.rows["org_b"])
        self.assertFalse(set(a) & set(b))
        self.assertNotEqual(self.dev.rows["org_a"][0]["Id"][3:5], self.dev.rows["org_b"][0]["Id"][3:5])


if __name__ == "__main__":
    unittest.main()
