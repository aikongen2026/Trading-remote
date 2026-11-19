"""Read-only HUD diagnostics stay readable and do not change execution guards."""
import unittest
import control_server


class MarketHudLayoutTests(unittest.TestCase):
    def test_market_data_hud_avoids_global_table_layout(self):
        page = control_server.HTML
        self.assertIn('class="hudDataRow"', page)
        self.assertIn('class="hudDataList"', page)
        self.assertIn('#hudQuickPanel .hudDataValue{min-width:0;', page)
        self.assertNotIn("innerHTML='<table>'+rows.join('')+'</table>'", page)

    def test_market_health_explains_stale_quotes_without_disabling_guard(self):
        page = control_server.HTML
        self.assertIn("startsWith('QUOTE_STALE')", page)
        self.assertIn("Tilkoblet strøm er ikke det samme som ferske kurser", page)
        self.assertIn("Sikkerhetssperren for nye handler beholdes", page)
        self.assertIn("health.gdelt", page)
        self.assertIn("health.sec", page)


if __name__ == '__main__':
    unittest.main()
