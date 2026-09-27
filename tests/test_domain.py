import datetime as dt
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from domain import AppError, Store

class LendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'test.db', timeout=.1)
        self.store.initialize()
        self.due = (dt.date.today()+dt.timedelta(days=7)).isoformat()

    def prepare(self, uid=1, device=1):
        return self.store.prepare(uid, device, self.due, '研修で使用')

    def expect(self, code, fn, *args):
        with self.assertRaises(AppError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.code,code)

    def assert_consistent(self, active=1):
        state = self.store.state(1)
        self.assertEqual(state['inconsistent'],0)
        self.assertEqual(state['active'],active)

    def test_normal_lend_and_return_persist_across_connections(self):
        loan = self.store.lend(1,self.prepare())
        self.assert_consistent(2)
        self.assertEqual(Store(self.store.path).receipt(1,loan)['returned_at'],None)
        self.assertEqual(self.store.return_device(1,loan),'PC-0001')
        self.assertIsNotNone(self.store.receipt(1,loan)['returned_at'])
        self.assert_consistent()

    def test_existing_lent_device(self):
        self.expect('E-01',self.prepare,1,2)
        self.assert_consistent()

    def test_past_today_invalid_and_required(self):
        yesterday = (dt.date.today()-dt.timedelta(days=1)).isoformat()
        for due,code in [(yesterday,'E-08'),(dt.date.today().isoformat(),'E-11'),('2026-02-30','E-10'),('','E-12')]:
            self.expect(code,self.store.prepare,1,1,due,'研修')
        for purpose,code in [('  ','E-13'),('あ'*101,'E-14')]:
            self.expect(code,self.store.prepare,1,1,self.due,purpose)
        self.assert_consistent()

    def test_employee_permission_rechecked_at_confirmation(self):
        for uid in (3,4):
            self.expect('E-02',self.prepare,uid)
        token=self.prepare()
        with self.store.connection() as con:
            con.execute("UPDATE employees SET employment_status='RETIRED' WHERE id=1")
        self.expect('E-02',self.store.lend,1,token)
        self.assert_consistent()

    def test_two_users_race_for_one_device(self):
        tokens=[self.prepare(1), self.prepare(2)]
        barrier=threading.Barrier(2)
        def attempt(uid):
            barrier.wait()
            try:
                self.store.lend(uid,tokens[uid-1])
                return 'OK'
            except AppError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,[1,2]))
        self.assertCountEqual(results,['OK','E-06'])
        self.assert_consistent(2)

    def test_double_submit_and_browser_back(self):
        token=self.prepare()
        barrier=threading.Barrier(2)
        def attempt(_):
            barrier.wait()
            try:
                self.store.lend(1,token)
                return 'OK'
            except AppError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt,range(2))),['OK','E-07'])
        self.expect('E-07',self.store.lend,1,token)
        self.assert_consistent(2)

    def test_separate_requests_from_same_user(self):
        first,second=self.prepare(),self.prepare()
        self.store.lend(1,first)
        self.expect('E-05',self.store.lend,1,second)
        self.assert_consistent(2)

    def test_database_unique_constraint_independent_of_application(self):
        with self.store.connection() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose,request_token) VALUES (2,1,?,?,?,?)',('now',self.due,'test','bypass'))
        self.assert_consistent()

    def test_lend_failure_rolls_back_both_tables_and_token(self):
        for fault,code in [('update','E-18'),('connection','E-19')]:
            token=self.prepare()
            self.expect(code,self.store.lend,1,token,fault)
            self.assert_consistent()
            self.assertEqual(self.store.confirmation(1,token)['consumed'],0)
            self.assertEqual(self.store.state(1)['devices'][0]['status'],'AVAILABLE')
        loan=self.store.lend(1,token)
        self.assert_consistent(2)
        self.assertGreater(loan,1)

    def test_return_failure_rolls_back_and_can_retry(self):
        loan=self.store.lend(1,self.prepare())
        for fault,code in [('update','E-18'),('connection','E-19')]:
            self.expect(code,self.store.return_device,1,loan,fault)
            self.assertIsNone(self.store.receipt(1,loan)['returned_at'])
            self.assert_consistent(2)
        self.store.return_device(1,loan)
        self.expect('E-16',self.store.return_device,1,loan)
        self.assert_consistent()

    def test_real_database_lock_timeout_leaves_no_partial_loan(self):
        token=self.prepare()
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            self.expect('E-19',self.store.lend,1,token)
            con.rollback()
        self.assert_consistent()
        self.store.lend(1,token)
        self.assert_consistent(2)

    def test_user_cannot_use_someone_elses_confirmation_or_return(self):
        token=self.prepare(2,3)
        self.expect('E-20',self.store.lend,1,token)
        self.expect('E-20',self.store.confirmation,1,token)
        self.expect('E-20',self.store.return_device,1,1)
        self.expect('E-20',self.store.receipt,1,1)
        self.assert_consistent()

    def test_tampered_confirmation_date_revalidated(self):
        token=self.prepare()
        with self.store.connection() as con:
            con.execute('UPDATE confirmations SET due_date=? WHERE token=?',((dt.date.today()-dt.timedelta(days=1)).isoformat(),token))
        self.expect('E-08',self.store.lend,1,token)
        self.assert_consistent()

if __name__=='__main__':
    unittest.main()
