"""貸出・返却の検証とSQLiteトランザクション。Web画面から独立してテストする。"""
import datetime as dt
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MESSAGES = {
    'E-01': 'この端末は現在貸出中です。別の端末を選択してください。',
    'E-02': '貸出申請の権限がありません。',
    'E-05': 'すでに申請済みです。',
    'E-06': '他のユーザーが先に予約しました。',
    'E-07': '送信済みの申請です。再度貸出処理を行うことはできません。',
    'E-08': '明日以降の日付を入力してください。',
    'E-10': '存在しない日付です。',
    'E-11': '明日以降の日付を入力してください。',
    'E-12': '返却予定日を入力してください。',
    'E-13': '利用目的を入力してください。',
    'E-14': '100文字以内で入力してください。',
    'E-16': 'この端末は借りておりません。',
    'E-18': 'データの更新に失敗しました。もう一度入力してください。',
    'E-19': '接続が切れました。更新して再度入力してください。',
    'E-20': '本人のIDを入力してください。',
    'INVALID': '指定されたデータが見つかりません。画面を開き直してください。',
}

class AppError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])

SCHEMA = '''
CREATE TABLE IF NOT EXISTS employees (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL,
 employment_status TEXT NOT NULL CHECK(employment_status IN ('ACTIVE','LEAVE','RETIRED')));
CREATE TABLE IF NOT EXISTS devices (
 id INTEGER PRIMARY KEY, asset_no TEXT NOT NULL UNIQUE, model_name TEXT NOT NULL,
 device_type TEXT NOT NULL DEFAULT 'LAPTOP',
 status TEXT NOT NULL CHECK(status IN ('AVAILABLE','LENT','REPAIR','DISPOSED')),
 purchased_at TEXT);
CREATE TABLE IF NOT EXISTS lendings (
 id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
 user_id INTEGER NOT NULL REFERENCES employees(id), lent_at TEXT NOT NULL,
 due_date TEXT NOT NULL, returned_at TEXT,
 purpose TEXT NOT NULL CHECK(length(purpose) BETWEEN 1 AND 100),
 request_token TEXT NOT NULL UNIQUE);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_lending_per_device
 ON lendings(device_id) WHERE returned_at IS NULL;
CREATE TABLE IF NOT EXISTS confirmations (
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES employees(id),
 device_id INTEGER NOT NULL REFERENCES devices(id), due_date TEXT NOT NULL,
 purpose TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL);
'''

class Store:
    def __init__(self, path, timeout=2):
        self.path = str(path)
        self.timeout = timeout

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys = ON')
        try:
            yield con
        finally:
            con.close()

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute('PRAGMA journal_mode = WAL')
            con.executescript(SCHEMA)
            con.execute('BEGIN IMMEDIATE')
            try:
                if not con.execute('SELECT 1 FROM employees LIMIT 1').fetchone():
                    con.executemany('INSERT INTO employees VALUES (?,?,?,?)', [
                        (1,'山田 太郎','開発部','ACTIVE'), (2,'佐藤 花子','企画部','ACTIVE'),
                        (3,'鈴木 一郎','営業部','LEAVE'), (4,'田中 美咲','管理部','RETIRED')])
                    con.executemany('INSERT INTO devices(id,asset_no,model_name,status) VALUES (?,?,?,?)', [
                        (1,'PC-0001','ThinkPad X1 Carbon','AVAILABLE'),
                        (2,'PC-0002','Dell Latitude 5440','LENT'),
                        (3,'PC-0003','HP EliteBook 840','AVAILABLE'),
                        (4,'PC-0004','ThinkPad T14','AVAILABLE')])
                    con.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose,request_token) VALUES (2,2,?,?,?,?)',
                                (dt.datetime.now().isoformat(timespec='seconds'), (dt.date.today()+dt.timedelta(days=7)).isoformat(), '社外研修', 'seed-loan'))
                con.commit()
            except Exception:
                con.rollback()
                raise

    @contextmanager
    def transaction(self):
        try:
            with self.connection() as con:
                try:
                    # 読み取り前に書き込みロックを確保。同時リクエストを順番に検証する。
                    con.execute('BEGIN IMMEDIATE')
                    yield con
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
        except AppError:
            raise
        except sqlite3.IntegrityError as exc:
            raise AppError('E-18') from exc
        except sqlite3.OperationalError as exc:
            raise AppError('E-19') from exc
        except sqlite3.DatabaseError as exc:
            raise AppError('E-18') from exc

    def state(self, user_id):
        with self.connection() as con:
            user = con.execute('SELECT * FROM employees WHERE id=?', (user_id,)).fetchone()
            if not user:
                raise AppError('E-20')
            devices = [dict(r) for r in con.execute('SELECT * FROM devices ORDER BY id')]
            loans = [dict(r) for r in con.execute('''SELECT l.*,d.asset_no,d.model_name FROM lendings l
                JOIN devices d ON d.id=l.device_id WHERE l.user_id=? AND l.returned_at IS NULL ORDER BY l.id DESC''', (user_id,))]
            active = con.execute('SELECT count(*) FROM lendings WHERE returned_at IS NULL').fetchone()[0]
            inconsistent = con.execute('''SELECT count(*) FROM devices d WHERE
                (d.status='LENT') != EXISTS(SELECT 1 FROM lendings l WHERE l.device_id=d.id AND l.returned_at IS NULL)''').fetchone()[0]
            return dict(user=user, devices=devices, loans=loans, active=active, inconsistent=inconsistent)

    @staticmethod
    def validate_input(due_date, purpose):
        if not due_date:
            raise AppError('E-12')
        try:
            day = dt.date.fromisoformat(due_date)
        except (ValueError, TypeError):
            raise AppError('E-10')
        if day < dt.date.today():
            raise AppError('E-08')
        if day == dt.date.today():
            raise AppError('E-11')
        if not purpose.strip():
            raise AppError('E-13')
        if len(purpose.strip()) > 100:
            raise AppError('E-14')

    @staticmethod
    def validate_employee(con, user_id):
        employee = con.execute('SELECT * FROM employees WHERE id=?', (user_id,)).fetchone()
        if not employee:
            raise AppError('E-20')
        if employee['employment_status'] != 'ACTIVE':
            raise AppError('E-02')

    def prepare(self, user_id, device_id, due_date, purpose):
        self.validate_input(due_date, purpose)
        with self.transaction() as con:
            self.validate_employee(con, user_id)
            device = con.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if not device:
                raise AppError('INVALID')
            if device['status'] != 'AVAILABLE':
                raise AppError('E-01')
            token = secrets.token_urlsafe(32)
            con.execute('INSERT INTO confirmations VALUES (?,?,?,?,?,0,?)',
                        (token,user_id,device_id,due_date,purpose.strip(),dt.datetime.now().isoformat()))
        return token

    def confirmation(self, user_id, token):
        with self.connection() as con:
            row = con.execute('''SELECT c.*,d.asset_no,d.model_name FROM confirmations c
                JOIN devices d ON d.id=c.device_id WHERE c.token=?''', (token,)).fetchone()
            if not row or row['user_id'] != user_id:
                raise AppError('E-20')
            return dict(row)

    @staticmethod
    def inject_failure(con, fault):
        # 開発用。1つ目の更新が実際に行われた後でSQLite自身にエラーを発生させる。
        if fault == 'update':
            con.execute("INSERT INTO devices(id,asset_no,model_name,status) VALUES (-1,'FAULT','FAULT','INVALID')")
        elif fault == 'connection':
            raise sqlite3.OperationalError('Simulated connection interruption before commit')

    def lend(self, user_id, token, fault=None):
        with self.transaction() as con:
            self.validate_employee(con, user_id)
            row = con.execute('SELECT * FROM confirmations WHERE token=?', (token,)).fetchone()
            if not row or row['user_id'] != user_id:
                raise AppError('E-20')
            if row['consumed']:
                raise AppError('E-07')
            # 確認画面を日付変更後に送信した場合も再検証する。
            self.validate_input(row['due_date'], row['purpose'])
            device = con.execute('SELECT * FROM devices WHERE id=?', (row['device_id'],)).fetchone()
            if device['status'] != 'AVAILABLE':
                prior = con.execute('SELECT user_id FROM lendings WHERE device_id=? AND returned_at IS NULL', (row['device_id'],)).fetchone()
                raise AppError('E-05' if prior and prior['user_id'] == user_id else 'E-06')
            now = dt.datetime.now().isoformat(timespec='seconds')
            cursor = con.execute('''INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose,request_token)
                VALUES (?,?,?,?,?,?)''', (row['device_id'],user_id,now,row['due_date'],row['purpose'],token))
            loan_id = cursor.lastrowid
            self.inject_failure(con, fault)
            updated = con.execute("UPDATE devices SET status='LENT' WHERE id=? AND status='AVAILABLE'", (row['device_id'],))
            if updated.rowcount != 1:
                raise AppError('E-06')
            con.execute('UPDATE confirmations SET consumed=1 WHERE token=?', (token,))
        return loan_id

    def return_device(self, user_id, loan_id, fault=None):
        with self.transaction() as con:
            loan = con.execute('SELECT * FROM lendings WHERE id=?', (loan_id,)).fetchone()
            if not loan:
                raise AppError('E-16')
            if loan['user_id'] != user_id:
                raise AppError('E-20')
            device = con.execute('SELECT * FROM devices WHERE id=?', (loan['device_id'],)).fetchone()
            if loan['returned_at'] or device['status'] != 'LENT':
                raise AppError('E-16')
            con.execute('UPDATE lendings SET returned_at=? WHERE id=?', (dt.datetime.now().isoformat(timespec='seconds'),loan_id))
            self.inject_failure(con, fault)
            con.execute("UPDATE devices SET status='AVAILABLE' WHERE id=?", (loan['device_id'],))
        return device['asset_no']

    def receipt(self, user_id, loan_id):
        with self.connection() as con:
            row = con.execute('''SELECT l.*,d.asset_no,d.model_name FROM lendings l JOIN devices d ON d.id=l.device_id
                WHERE l.id=? AND l.user_id=?''', (loan_id,user_id)).fetchone()
            if not row:
                raise AppError('E-20')
            return dict(row)
