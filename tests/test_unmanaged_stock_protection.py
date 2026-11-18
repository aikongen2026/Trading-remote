"""Read-only protection diagnostics for unknown positions, including AEP.

No broker order may be sent, cancelled or modified by these tests.
"""
import unittest

from runtime_audit import supervise_stocks


class ReadOnlyApi:
    def __getattr__(self, name):
        raise AssertionError("Unexpected broker API action: " + name)


class ReadOnlyGuard:
    def __getattr__(self, name):
        raise AssertionError("Unknown holdings cannot be managed: " + name)


class UnmanagedStockTests(unittest.TestCase):
    def run_case(self, extra_open_orders=None):
        positions=[{"symbol":"AEP","qty":"2","asset_class":"us_equity",
                    "avg_entry_price":"131.43"}]
        result=supervise_stocks(
            ReadOnlyApi(),positions,[],{},ReadOnlyGuard(),
            {"armed":True,"kill":False,"test_mode":False,
             "broker_bracket_enabled":True},
            {"is_open":True},None,open_orders=extra_open_orders or [])
        self.assertEqual(result["unknown_positions"],["AEP"])
        self.assertEqual(result["actions"],{})
        return result["unmanaged_positions"]["AEP"]

    def test_missing_stop_is_explicit(self):
        self.assertEqual(self.run_case()["protection_status"],"NO_STOP_ORDER")

    def test_open_stop_protects_observed_qty_not_ownership(self):
        data=self.run_case([{"id":"s1","symbol":"AEP","side":"sell",
                             "type":"stop","status":"new","qty":"2",
                             "filled_qty":"0","stop_price":"120.0"}])
        self.assertEqual(data["protection_status"],"STOP_ORDER_PRESENT")
        self.assertEqual(data["open_stop_qty"],2)

    def test_partial_stop_is_not_safe(self):
        data=self.run_case([{"id":"s1","symbol":"AEP","side":"sell",
                             "type":"stop_limit","status":"new","qty":"1",
                             "filled_qty":"0"}])
        self.assertEqual(data["protection_status"],"STOP_ORDER_PARTIAL")

    def test_expired_stop_is_not_counted(self):
        data=self.run_case([{"id":"s1","symbol":"AEP","side":"sell",
                             "type":"stop","status":"canceled","qty":"2"}])
        self.assertEqual(data["protection_status"],"NO_STOP_ORDER")

    def test_same_order_in_two_sources_not_double_counted(self):
        data=self.run_case([{"id":"s1","symbol":"AEP","side":"sell",
                             "type":"stop","status":"new","qty":"1"},
                            {"id":"s1","symbol":"AEP","side":"sell",
                             "type":"stop","status":"new","qty":"1"}])
        self.assertEqual(data["protection_status"],"STOP_ORDER_PARTIAL")
        self.assertEqual(data["open_stop_qty"],1)

    def test_other_symbol_stop_does_not_protect_aep(self):
        data=self.run_case([{"id":"s2","symbol":"AAPL","side":"sell",
                             "type":"stop","status":"new","qty":"2"}])
        self.assertEqual(data["protection_status"],"NO_STOP_ORDER")


if __name__=="__main__":
    unittest.main()
