"""Regression tests for order recovery, independent exits, audit and entry gating."""
import json
import io
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import trading_bot
import trade_export
from profit_engine import ProfitEngine
from intelligence_engine import SignalJournal
from runtime_audit import RuntimeAudit, EntryConfirmation, supervise_stocks, recover_stock_entry, read_evidence, storage_health


def iso(n):
    return datetime.fromtimestamp(n, timezone.utc).isoformat()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.pos = {'symbol': 'MSFT', 'qty': '1', 'avg_entry_price': '540'}
        self.entry = {'id': 'entry', 'symbol': 'MSFT', 'side': 'buy', 'filled_qty': '1',
                      'filled_at': iso(self.now-3600), 'client_order_id': 'tbe-sv450-MSFT-abc',
                      'legs': [{'id': 'stop', 'symbol': 'MSFT', 'side': 'sell', 'type': 'stop',
                                'status': 'new', 'filled_qty': '0', 'stop_price': '538.5'}]}
        self.guard = Mock()
        self.guard.get_exit.return_value = None
        self.guard.advance_exit.return_value = 'EXIT_SUBMITTED'
        self.control = {'armed': True, 'max_hold_sec': 1800, 'stock_flatten_before_close_min': 5}
        self.audit = RuntimeAudit(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_supervisor(self, **kwargs):
        return supervise_stocks(Mock(), [self.pos], [self.entry], {}, self.guard, self.control,
                                 {'is_open': True, 'next_close': iso(self.now+3600)}, self.audit,
                                 now=self.now, **kwargs)

    def test_restart_recovers_actual_fill_and_exits_without_bars(self):
        result = self.run_supervisor()
        self.assertEqual(result['actions']['MSFT']['reason'], 'MAX_HOLD')
        self.assertEqual(result['actions']['MSFT']['held_sec'], 3600)
        self.assertEqual(self.guard.advance_exit.call_count, 1)

    def test_pause_still_allows_protective_exit(self):
        self.control['paused'] = True
        self.assertIn('MSFT', self.run_supervisor()['actions'])

    def test_kill_does_not_submit(self):
        self.control['kill'] = True
        self.assertEqual(self.run_supervisor()['actions'], {})
        self.guard.advance_exit.assert_not_called()

    def test_manual_position_is_not_adopted(self):
        self.entry['client_order_id'] = 'manual'
        self.assertEqual(self.run_supervisor()['unknown_positions'], ['MSFT'])
        self.guard.advance_exit.assert_not_called()

    def test_fifo_partial_exit_uses_surviving_lot(self):
        newer = {**self.entry, 'id': 'newer', 'filled_at': iso(self.now-50)}
        sold = {'id': 'sold', 'symbol': 'MSFT', 'side': 'sell', 'filled_qty': '1', 'filled_at': iso(self.now-30)}
        at, order = recover_stock_entry('MSFT', self.pos, [self.entry, newer, sold], {})
        self.assertEqual(order['id'], 'newer')
        self.assertAlmostEqual(at, self.now-50, places=4)

    def test_close_uses_broker_clock(self):
        self.entry['filled_at'] = iso(self.now-60)
        result = supervise_stocks(Mock(), [self.pos], [self.entry], {}, self.guard, self.control,
                                  {'is_open': True, 'next_close': iso(self.now+240)}, self.audit, now=self.now)
        self.assertEqual(result['actions']['MSFT']['reason'], 'CLOSE_FLATTEN')

    def test_missing_stop_on_owned_position_exits(self):
        self.entry['filled_at'] = iso(self.now-60)
        self.entry['legs'][0]['status'] = 'expired'
        self.assertEqual(self.run_supervisor()['actions']['MSFT']['reason'], 'MISSING_PROTECTION')

    def test_market_closed_does_not_cancel_protection_or_submit(self):
        result = supervise_stocks(Mock(), [self.pos], [self.entry], {}, self.guard, self.control,
                                  {'is_open': False}, self.audit, now=self.now)
        self.assertEqual(result['actions'], {})

    def test_missing_version_function_and_context_logging_regression(self):
        journal = SignalJournal(str(Path(self.temp.name)/'learning_v3.sqlite'))
        intel = trading_bot.intelligence_from_status({'debug': {'effective_strategy': 'trend', 'router': {'mode':'trend'}}, 'price': 100})
        journal.log({'symbol': 'MSFT', 'market': 'STOCK', 'price': 100, 'bot_version': intel['bot_version'],
                     'context_json': intel['context_json']})
        self.assertEqual(journal.last_error, '')
        self.assertTrue(journal.last_write)
        with closing(sqlite3.connect(journal.path)) as db:
            version, raw = db.execute('SELECT bot_version,context_json FROM decisions').fetchone()
        self.assertEqual(version, trading_bot.runtime_bot_version())
        self.assertEqual(json.loads(raw)['strategy'], 'trend')

    def test_next_day_is_not_labelled_as_one_hour_outcome(self):
        journal = SignalJournal(str(Path(self.temp.name)/'learning_v3.sqlite'))
        journal.log({'symbol': 'MSFT', 'price': 100, 'ts': iso(self.now-86400)})
        journal.update_outcomes('MSFT', 105)
        with closing(sqlite3.connect(journal.path)) as db:
            self.assertEqual(db.execute('SELECT outcome_60m FROM decisions').fetchone()[0], None)

    def test_calibration_does_not_count_repeated_buy_snapshots(self):
        journal = SignalJournal(str(Path(self.temp.name)/'learning_v3.sqlite'))
        for event in ('ENTRY', 'SNAPSHOT', 'SNAPSHOT'):
            journal.log({'symbol':'MSFT', 'market':'STOCK', 'signal':1, 'price':100,
                         'action':'BUY 1', 'event_type':event, 'probability_up':0.6})
        with closing(sqlite3.connect(journal.path)) as db, db:
            db.execute('UPDATE decisions SET outcome_15m=0.001')
        summary = ProfitEngine(self.temp.name).calibration_summary('STOCK')
        self.assertEqual(summary['samples'], 1)

    def test_local_bars_and_risk_events_survive_new_instance(self):
        self.audit.event('RISK_EXIT', 'MSFT', {'reason': 'MAX_HOLD'})
        self.audit.capture_bars({'MSFT':[{'t':iso(self.now), 'c':540, 'h':541, 'l':539}]}, 'iex')
        evidence = read_evidence(self.temp.name, iso(self.now-10), iso(time.time()+10))
        self.assertEqual(len(evidence['events']), 1)
        self.assertEqual(len(evidence['bars']['MSFT']), 1)
        self.assertEqual(evidence['errors'], [])

    def test_auto_feed_resolved_for_analysis(self):
        api = Mock()
        api.cfg.stock_feed = 'auto'
        api.resolve_stock_feed.return_value = 'iex'
        api.bars.return_value = {'MSFT': []}
        result, errors = trade_export._fetch_trade_bars(api, [{'symbol': 'MSFT'}],
                                                       {'range_start_utc':iso(self.now-86400),'range_end_utc':iso(self.now)}, 1)
        self.assertEqual(api.bars.call_args.args[-1], 'iex')
        self.assertEqual(errors, [])

    def test_candidate_requires_continuity_and_resets_on_gap(self):
        gate = EntryConfirmation()
        for n in range(15):
            self.assertFalse(gate.ready('MSFT', 'trend', True, n))
        self.assertTrue(gate.ready('MSFT', 'trend', True, 15))
        self.assertFalse(gate.ready('MSFT', 'trend', True, 300))
        self.assertFalse(gate.ready('MSFT', 'trend', False, 301))
        self.assertFalse(gate.ready('MSFT', 'meanrev', True, 302))

    def test_render_writable_directory_is_not_assumed_persistent(self):
        with patch.dict(os.environ, {'RENDER':'true'}), patch('os.path.ismount', return_value=False):
            self.assertFalse(storage_health(self.temp.name)['persistent'])

    def test_full_engine_supervises_even_when_symbol_is_outside_universe_and_bars_missing(self):
        from types import SimpleNamespace
        api = Mock()
        api.resolve_stock_feed.return_value = 'iex'
        api.account.return_value = {'equity':'100000', 'last_equity':'100000'}
        api.positions.return_value = [self.pos]
        api.get_orders.side_effect = lambda limit, status, nested: [self.entry] if status == 'all' else []
        api.clock.return_value = {'is_open': True, 'next_close': iso(self.now+3600)}
        api.portfolio_history.return_value = {}
        api.quotes_latest.return_value = {}
        api.bars.return_value = {}
        cfg = SimpleNamespace(paper=True, stock_feed='iex', trading_base='test', data_base='test')
        intel = Mock()
        intel.stock_stream_symbols = []
        self.guard.summary.return_value = {}
        intel.journal = SignalJournal(str(Path(self.temp.name)/'learning_v3.sqlite'))
        control = {**trading_bot.DEFAULT_CONTROL, **self.control, 'market_mode':'stocks', 'symbols':['AAPL'],
                   'intelligence_enabled':False, 'profit_engine_enabled':False, 'dynamic_universe_enabled':False,
                   'champion_challenger_enabled':False, 'test_mode':False}
        status_path = str(Path(self.temp.name)/'status.json')
        # Stop after one full loop, without invoking real sleeps or any broker service.
        with patch.multiple(trading_bot, STATE_DIR=self.temp.name, STATUS_PATH=status_path,
                            CONTROL_PATH=str(Path(self.temp.name)/'control.json'),
                            TRADE_PLANS_PATH=str(Path(self.temp.name)/'plans.json')), \
             patch.object(trading_bot, 'acquire_single_instance_socket'), \
             patch.object(trading_bot, 'load_cfg', return_value=cfg), \
             patch.object(trading_bot, 'AlpacaRest', return_value=api), \
             patch.object(trading_bot, 'IntelligenceEngine', return_value=intel), \
             patch.object(trading_bot, 'ExecutionGuard', return_value=self.guard), \
             patch('broker_history.BrokerHistory.start'), \
             patch.object(trading_bot, 'ensure_control_file', return_value=control), \
             patch('trading_bot.time.sleep', side_effect=KeyboardInterrupt), \
             patch('sys.stdout', new=io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                trading_bot.main()
        status = json.loads(Path(status_path).read_text(encoding='utf-8'))
        self.assertEqual(status['engine_state'], 'RUNNING', status.get('notes'))
        self.assertEqual(status['risk_supervision']['actions']['MSFT']['reason'], 'MAX_HOLD')
        self.guard.advance_exit.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
