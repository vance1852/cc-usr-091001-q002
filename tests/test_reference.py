import json
import unittest
from decimal import Decimal
from pathlib import Path


class WarrantyReferenceTest(unittest.TestCase):
    def test_meter_change_has_two_distinct_endpoints(self):
        data = json.loads((Path(__file__).parents[1] / "reference" / "warranty_case.json").read_text(encoding="utf-8"))
        change = data["meter_change"]
        self.assertNotEqual(change["old_meter"]["id"], change["new_meter"]["id"])
        self.assertGreater(Decimal(change["old_meter"]["final_kwh"]), Decimal("0"))
        self.assertTrue(data["source_digest"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
