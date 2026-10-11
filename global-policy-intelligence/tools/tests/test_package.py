"""Check the installed skill directory and its referenced local resources."""
from pathlib import Path
import re
import sys
import unittest

import yaml

TOOLS = Path(__file__).resolve().parents[1]
SKILL = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from collector.cli import DEPS


class PackageTests(unittest.TestCase):
    def test_skill_root_is_self_contained(self):
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        header = yaml.safe_load(content.split("---", 2)[1])
        self.assertEqual(header["name"], SKILL.name)
        self.assertTrue(header["description"])
        for resource in ("requirements.txt", "LICENSE", "tools/collector/__main__.py"):
            self.assertTrue((SKILL / resource).is_file())

    def test_document_local_links_exist(self):
        documents = [SKILL / "SKILL.md", *list((SKILL / "references").glob("*.md"))]
        for path in documents:
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                resource = target.split("#", 1)[0]
                self.assertTrue((path.parent / resource).is_file(), f"{path.name}: {target}")

    def test_doctor_and_requirements_agree_on_collect_dependencies(self):
        requirements = (SKILL / "requirements.txt").read_text(encoding="utf-8")
        distributions = {re.split(r"[<>=!~\s]", line)[0].casefold()
                         for line in requirements.splitlines() if line and not line.startswith("#")}
        self.assertEqual(distributions, {name.casefold() for name, required, _ in DEPS.values() if required})


if __name__ == "__main__":
    unittest.main()
