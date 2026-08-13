from pathlib import Path
import unittest

from src.monitor import PageKind, classify_tour_page


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
	return (FIXTURES / name).read_text(encoding="utf-8")


class PageClassificationTests(unittest.TestCase):
	def test_exact_observed_block_is_no_tours(self):
		result = classify_tour_page(load_fixture("no_tours.html"))
		self.assertEqual(PageKind.NO_TOURS, result.kind)

	def test_negative_phrase_plus_extra_content_is_not_no_tours(self):
		source = load_fixture("no_tours.html").replace(
			"В настоящий момент доступных экскурсий нет",
			"В настоящий момент доступных экскурсий нет <p>Дополнительный текст</p>",
		)
		self.assertEqual(PageKind.UNEXPECTED_FORMAT, classify_tour_page(source).kind)

	def test_actionable_tour_is_available(self):
		result = classify_tour_page(load_fixture("tours_available.html"))
		self.assertEqual(PageKind.TOURS_AVAILABLE, result.kind)
		self.assertIn("15 августа, 14:00", result.text)
		self.assertIn("Обзорная экскурсия", result.text)

	def test_missing_boundary_is_unexpected(self):
		result = classify_tour_page(
			"<h1>Записаться на экскурсию</h1><p>Технические работы</p>"
		)
		self.assertEqual(PageKind.UNEXPECTED_FORMAT, result.kind)
		self.assertTrue(result.fingerprint)


if __name__ == "__main__":
	unittest.main()
