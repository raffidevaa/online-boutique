import unittest

from research.service import ServiceCatalog
from research.utils import ResearchConfig


class CatalogTests(unittest.TestCase):
    def test_catalog_has_known_confounders_and_optional_assistant(self) -> None:
        catalog = ServiceCatalog.load(ResearchConfig().service_metadata)
        self.assertIn("JVM", catalog.get("adservice").confounders[0])
        self.assertFalse(catalog.get("shoppingassistantservice").deployed)
        self.assertFalse(catalog.get("loadgenerator").targetable)

    def test_catalog_dependencies_reference_catalog_services(self) -> None:
        catalog = ServiceCatalog.load(ResearchConfig().service_metadata)
        for service in catalog.services.values():
            self.assertTrue(set(service.dependencies).issubset(catalog.services))

if __name__ == "__main__":
    unittest.main()
