import unittest

from pr_harness.core.finding_identity import repair_rule_id
from pr_harness.core.models import Finding, Severity
from pr_harness.evaluation.harness import FixtureRepairer
from pr_harness.repair.rule_fixer import SafeFixer


class RepairIdentityTests(unittest.TestCase):
    def test_model_rule_uses_unique_cwe_and_preserves_legacy_names(self):
        self.assertEqual("SEC-SQL-CONCAT", repair_rule_id("py/sql-injection", "CWE-89"))
        self.assertEqual("REL-DEBUG-PRINT", repair_rule_id("REL-DEBUG-PRINT"))
        self.assertEqual("UNKNOWN", repair_rule_id("unknown"))
        self.assertEqual("MODEL-DESERIALIZE", repair_rule_id("model-deserialize", "CWE-502"))

    def test_production_ast_repair_accepts_model_secret_identity(self):
        finding = {"path": "app.py", "line": 1, "rule_id": "model-secret", "cwe": "CWE-798"}
        result = SafeFixer().apply('password = "example-secret"\n', [finding], "app.py")
        self.assertIn("os.environ", result["content"])
        self.assertIn("PASSWORD", result["content"])
        self.assertEqual(["SEC-HARDCODED-SECRET"], result["rules"])
        self.assertEqual("model-secret", finding["rule_id"])

    def test_fixture_sql_repair_accepts_model_identity(self):
        finding = Finding(
            "py/sql-injection", Severity.HIGH, "SQL injection", "Untrusted query input",
            "app.py", 2, 'cursor.execute("SELECT * FROM users WHERE id=" + value)',
            "Parameterize the query", "Test an injection payload", cwe="CWE-89",
        )
        case = {
            "after_files": {"app.py": 'def process(value):\n    cursor.execute("SELECT * FROM users WHERE id=" + value)\n'},
            "repair_validation": {
                "auto_fixable": True, "risk_pattern": r"\+ value",
                "required_after_patterns": [r"\(value,\)"],
            },
        }
        result = FixtureRepairer().repair(case, finding)
        self.assertTrue(result["passed"], result["checks"])
        self.assertEqual("py/sql-injection", finding.rule_id)


if __name__ == "__main__":
    unittest.main()
