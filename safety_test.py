from __future__ import annotations

import tempfile
import shutil
from pathlib import Path

from execution_guard import ExecutionGuard


class FakeAPI:
    def __init__(self):
        self.submits=[]
        self.cancels=[]
        self.by_client={}
        self.timeout_after_accept=False

    def cancel_order(self, oid):
        self.cancels.append(oid)
        return None

    def submit_order(self, symbol, side, qty, tif='day', **kwargs):
        cid=kwargs.get('client_order_id','')
        row={
            'id':f'ord-{len(self.submits)+1}', 'client_order_id':cid, 'symbol':symbol,
            'side':side, 'qty':str(qty), 'filled_qty':'0', 'status':'new', 'type':kwargs.get('order_type','market')
        }
        self.submits.append(row.copy())
        self.by_client[cid]=row.copy()
        if self.timeout_after_accept:
            self.timeout_after_accept=False
            raise TimeoutError('simulated network timeout after broker accepted order')
        return row

    def get_order_by_client_order_id(self, cid):
        return self.by_client.get(cid)


def main():
    root=tempfile.mkdtemp(prefix='tradingbot_execution_guard_')
    try:
        api=FakeAPI()
        g=ExecutionGuard(root)

        # Entry latch survives restart and only rearms after explicit reset.
        assert not g.entry_blocked('STOCK','AAPL')
        g.block_entry('STOCK','AAPL','entry_submitted','tbe-s-AAPL-1')
        assert g.entry_blocked('STOCK','AAPL')
        g2=ExecutionGuard(root)
        assert g2.entry_blocked('STOCK','AAPL')
        g2.rearm_entry('STOCK','AAPL')
        assert not g2.entry_blocked('STOCK','AAPL')

        # Existing bracket protection is cancelled first; no competing market sell in same cycle.
        bracket=[{'id':'tp1','symbol':'AAPL','side':'sell','status':'new','type':'limit','client_order_id':'broker-leg'}]
        a=g2.advance_exit(api, bracket, market='STOCK', symbol='AAPL', position_qty=2, reason='SIGNAL_FLIP', regular_market_open=True)
        assert a=='EXIT_CANCEL_PROTECTION', a
        assert api.cancels==['tp1']
        assert len(api.submits)==0

        # Once broker confirms protection is gone, submit exactly one exit.
        a=g2.advance_exit(api, [], market='STOCK', symbol='AAPL', position_qty=2, reason='SIGNAL_FLIP', regular_market_open=True)
        assert a=='EXIT_SUBMITTED', a
        assert len(api.submits)==1
        current=[api.submits[0]]
        a=g2.advance_exit(api, current, market='STOCK', symbol='AAPL', position_qty=2, reason='SIGNAL_FLIP', regular_market_open=True)
        assert a=='EXIT_PENDING', a
        assert len(api.submits)==1, 'duplicate sell was submitted'

        # Partial fill remains pending; still no duplicate sell.
        partial=dict(api.submits[0]); partial['status']='partially_filled'; partial['filled_qty']='1'
        a=g2.advance_exit(api, [partial], market='STOCK', symbol='AAPL', position_qty=1, reason='SIGNAL_FLIP', regular_market_open=True)
        assert a=='EXIT_PENDING', a
        assert len(api.submits)==1

        # Position closed clears the persistent exit intent.
        a=g2.advance_exit(api, [partial], market='STOCK', symbol='AAPL', position_qty=0, reason='POSITION_CLOSED', regular_market_open=True)
        assert a=='EXIT_DONE'
        assert g2.get_exit('STOCK','AAPL') is None

        # Network timeout after broker acceptance: persist client id, reconcile it, do NOT resend.
        api.timeout_after_accept=True
        a=g2.advance_exit(api, [], market='CRYPTO', symbol='BTC/USD', position_qty=0.01, reason='STOP_LOSS')
        assert a=='EXIT_SUBMIT_UNKNOWN', a
        assert len(api.submits)==2
        # The fake broker accepted it and lookup finds it by client id on next cycle.
        a=g2.advance_exit(api, [], market='CRYPTO', symbol='BTC/USD', position_qty=0.01, reason='STOP_LOSS')
        assert a in {'EXIT_PENDING','EXIT_RECONCILING'}, a
        assert len(api.submits)==2, 'timeout caused duplicate sell'

        print('OK: entry latch survives restart')
        print('OK: bracket protection cancelled before intelligent exit')
        print('OK: one exit order per symbol')
        print('OK: partial fill does not duplicate exit')
        print('OK: timeout reconciles by client_order_id without resend')
        print('SAFETY TEST PASSED')
    finally:
        shutil.rmtree(root, ignore_errors=True)

if __name__=='__main__':
    main()
