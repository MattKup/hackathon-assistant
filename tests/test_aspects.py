import unittest

from app.answer.aspects import match_rules, parse_rules, rules_to_lines

R = [{"when": ["госзаказчик", "защита информации"], "note": "совет"}]


class AspectsTest(unittest.TestCase):
    def test_match_forms(self):
        self.assertTrue(match_rules("Для госзаказчика какие сроки?", R))
        self.assertTrue(match_rules("Нужна ли лицензия для работ по защите информации?", R))
        self.assertFalse(match_rules("За сколько запускать заявку на расчёт?", R))

    def test_parse_roundtrip(self):
        lines = rules_to_lines(R)
        self.assertEqual(parse_rules(lines), R)
        self.assertEqual(parse_rules(["без стрелки"]), [])
