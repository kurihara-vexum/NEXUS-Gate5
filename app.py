"""ローカル提出用Webアプリ。Python 3.10以降、外部パッケージ不要。"""
import argparse
import datetime as dt
import html
import secrets
import sqlite3
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from domain import Store, AppError

ROOT = Path(__file__).parent

def esc(value):
    return html.escape(str(value), quote=True)

def hidden(name, value):
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'

class Server(ThreadingHTTPServer):
    def __init__(self, address, store, demo=False):
        super().__init__(address, Handler)
        self.store, self.demo = store, demo
        self.sessions, self.session_lock = {}, threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # URLの確認トークンをログへ残さない。
        pass

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            pass
        sid = cookie['gate5'].value if 'gate5' in cookie else ''
        self.new_cookie = None
        with self.server.session_lock:
            if sid not in self.server.sessions:
                sid = secrets.token_urlsafe(32)
                # 実在の社員認証ではなく、課題用固定アカウントをサーバーで割り当てる。
                self.server.sessions[sid] = {'user_id': 1, 'csrf': secrets.token_urlsafe(32), 'fault': None, 'lock': threading.RLock()}
                self.new_cookie = sid
            self.sess = self.server.sessions[sid]

    def send(self, body, status=200, kind='text/html; charset=utf-8', location=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if self.new_cookie:
            self.send_header('Set-Cookie', f'gate5={self.new_cookie}; HttpOnly; SameSite=Strict; Path=/')
        if location:
            self.send_header('Location', location)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send('', 303, location=location)

    def form_fields(self):
        return hidden('csrf', self.sess['csrf']) + hidden('user_id', self.sess['user_id'])

    def layout(self, title, content, active='lend'):
        user = self.server.store.state(self.sess['user_id'])['user']
        nav = [('lend','/','貸出申請'), ('return','/returns','借りている端末')]
        if self.server.demo:
            nav.append(('demo','/demo','動作確認'))
        links = ''.join(f'<a class="{"active" if key==active else ""}" href="{url}">{label}</a>' for key,url,label in nav)
        return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} | NEXUS</title><link rel="stylesheet" href="/static/style.css"><script defer src="/static/app.js"></script></head><body>
<header><a class="brand" href="/"><svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#285ee8"/><path d="M9 23V9l14 14V9" stroke="white" stroke-width="3" stroke-linejoin="round"/></svg>NEXUS<small>IT資産管理</small></a>
<div class="user">{esc(user['name'])}<small>{esc(user['department'])} · 社員ID {user['id']}</small></div></header><nav>{links}</nav><main>{content}</main>
<footer><span>NEXUS · PC貸出管理</span><span>社内共用端末</span></footer></body></html>'''

    @staticmethod
    def alert(error):
        extra = '入力内容を確認して、もう一度お試しください。'
        if error.code in ('E-18','E-19'):
            extra = '処理は完了していません。今回の処理途中の更新は保存されていません。貸出状況を確認してから再度お試しください。'
        elif error.code in ('E-01','E-06'):
            extra = '貸出状況が変更されています。利用可能な端末を選択してください。'
        return f'<div class="alert" role="alert"><span class="error-code">{error.code}</span><div><strong>{esc(error)}</strong><p>{extra}</p></div></div>'

    def lending_page(self, values=None, error=None):
        values = values or {}
        state = self.server.store.state(self.sess['user_id'])
        due = values.get('due_date', (dt.date.today()+dt.timedelta(days=7)).isoformat())
        options = '<option value="">端末を選択してください</option>'
        devices_html = ''
        for d in state['devices']:
            label = '貸出可能' if d['status']=='AVAILABLE' else '貸出中' if d['status']=='LENT' else '貸出対象外'
            selected = ' selected' if str(d['id']) == str(values.get('device_id','')) else ''
            # 使用中の端末も表示する仕様。送信時にサーバーで必ず状態を再確認する。
            options += f'<option value="{d["id"]}"{selected}>{esc(d["asset_no"])} / {esc(d["model_name"])} ({label})</option>'
            devices_html += f'<div class="device"><div class="device-top"><strong>{esc(d["asset_no"])}</strong><span class="badge {"available" if d["status"]=="AVAILABLE" else "lent"}">{label}</span></div><p>{esc(d["model_name"])}</p></div>'
        content = '<p class="eyebrow">PC LENDING</p><h1>共用PCを借りる</h1><p class="lead">端末と利用期間を選んで、貸出を申請してください。</p>'
        content += '<div class="steps"><span class="current"><b>1</b>申請内容の入力</span><span><b>2</b>内容の確認</span><span><b>3</b>貸出完了</span></div>'
        if error:
            content += self.alert(error)
        content += f'''<div class="grid"><section class="card"><div class="card-head"><h2>貸出申請</h2><span class="hint">すべて必須項目</span></div>
<form method="post" action="/prepare" novalidate data-lock>{self.form_fields()}
<div class="field"><label for="device">機種・端末<span class="req">必須</span></label><select id="device" name="device_id">{options}</select></div>
<div class="field"><label for="due">返却予定日<span class="req">必須</span></label><input id="due" name="due_date" type="date" value="{esc(due)}"><p class="hint">明日以降の日付を指定してください。</p></div>
<div class="field"><label for="purpose">利用目的<span class="req">必須</span></label><textarea id="purpose" name="purpose" placeholder="例：社外研修で使用">{esc(values.get('purpose',''))}</textarea><p class="hint">100文字以内で入力してください。</p></div>
<div class="actions"><button type="submit" data-label="申請内容を確認する">申請内容を確認する <span aria-hidden="true">→</span></button></div></form></section>
<aside class="card"><h2>端末の貸出状況</h2>{devices_html}<p class="note">貸出状況は確定時にも確認します。</p></aside></div>'''
        return self.layout('貸出申請',content)

    def confirmation_page(self, token, error=None):
        row = self.server.store.confirmation(self.sess['user_id'],token)
        content = '<p class="eyebrow">CONFIRMATION</p><h1>申請内容の確認</h1><p class="lead">以下の内容で貸出を確定します。</p>'
        content += '<div class="steps"><span><b>1</b>申請内容の入力</span><span class="current"><b>2</b>内容の確認</span><span><b>3</b>貸出完了</span></div>'
        if error:
            content += self.alert(error)
        content += f'<section class="card"><h2>貸出内容</h2>{self.details(row)}<form action="/lend" method="post" data-lock>{self.form_fields()}{hidden("token",token)}<div class="actions"><button type="submit" data-label="確定">確定</button><a class="button secondary" href="/">入力画面に戻る</a></div></form></section>'
        return self.layout('申請内容の確認', content)

    @staticmethod
    def details(row):
        fields = [('端末',row['asset_no']),('機種',row['model_name']),('返却予定日',row['due_date'].replace('-','/')),('利用目的',row['purpose'])]
        return '<dl class="details">'+''.join(f'<div><dt>{name}</dt><dd>{esc(value)}</dd></div>' for name,value in fields)+'</dl>'

    def returns_page(self, error=None, returned=None):
        state = self.server.store.state(self.sess['user_id'])
        content = '<p class="eyebrow">MY DEVICES</p><h1>借りている端末</h1><p class="lead">使用を終えた端末を返却してください。</p>'
        if error:
            content += self.alert(error)
        if returned:
            content += f'<div class="toast" role="status">{esc(returned)} を返却しました。</div>'
        content += '<section class="card"><h2>貸出中の端末</h2>'
        for row in state['loans']:
            content += f'''<div class="loan"><div class="device-top"><h2>{esc(row['asset_no'])} / {esc(row['model_name'])}</h2><span class="badge lent">貸出中</span></div><p>返却予定日：{esc(row['due_date'])}　利用目的：{esc(row['purpose'])}</p>
<form method="post" action="/return" data-lock>{self.form_fields()}{hidden('loan_id',row['id'])}<button type="submit" class="secondary" data-label="返却する">返却する</button></form></div>'''
        if not state['loans']:
            content += '<div class="empty">現在、借りている端末はありません。</div>'
        return self.layout('借りている端末',content+'</section>','return')

    def demo_page(self):
        state = self.server.store.state(self.sess['user_id'])
        content = '<p class="eyebrow">DEMO WORKSPACE</p><h1>動作確認</h1><p class="lead">条件を準備した後、申請画面から実際に操作して確認します。撮影は各結果画面で行ってください。</p>'
        content += '<section class="card"><h2>提出用の4つの状態</h2>'
        scenarios = [('normal','正常時の貸出完了','利用可能な端末で申請し、確認画面で「確定」を押します。'),('lent','貸出中の端末','貸出中の端末を選んで、状態チェックを確認します。'),('past','過去の返却予定日','昨日の日付で申請し、入力エラーを確認します。'),('failure','更新途中の失敗','確認画面の確定時に1つ目の更新後で失敗させ、全体を取り消します。')]
        for i,(key,title,desc) in enumerate(scenarios,1):
            content += f'<form class="scenario" method="post" action="/demo/scenario">{self.form_fields()}{hidden("scenario",key)}<span class="num">0{i}</span><div><strong>{title}</strong><p>{desc}</p></div><button class="secondary" type="submit">準備する</button></form>'
        content += '</section><section class="card"><h2>データの確認</h2><p class="hint">失敗前後で件数と端末状態を確認できます。実際のデータベースを読み取っています。</p>'
        content += f'<div class="metrics"><div><strong>{state["active"]}</strong><span>未返却の貸出件数</span></div><div><strong>{sum(d["status"]=="LENT" for d in state["devices"])}</strong><span>貸出中の端末台数</span></div><div><strong>{state["inconsistent"]}</strong><span>状態の不整合件数</span></div></div></section>'
        content += f'''<section class="card"><h2>社員状態・同時操作の確認</h2><form action="/demo/user" method="post">{self.form_fields()}<label for="demo-user">操作する社員</label><select name="employee_id" id="demo-user">'''
        for uid,name in [(1,'山田 太郎（在職）'),(2,'佐藤 花子（在職）'),(3,'鈴木 一郎（休職）'),(4,'田中 美咲（退職）')]:
            content += f'<option value="{uid}">{name}</option>'
        content += '</select><div class="actions"><button class="secondary" type="submit">切り替える</button></div></form><p class="note">別ブラウザでそれぞれ同じ端末の確認画面を開き、順に確定すると競合エラーを確認できます。これはローカル検証専用の社員切替です。</p></section>'
        fault = '次の貸出・返却で更新失敗を再現します。' if self.sess['fault'] else '障害の再現は無効です。'
        content += f'<section class="card"><h2>障害の再現</h2><p class="hint">{fault}</p><form method="post" action="/demo/fault">{self.form_fields()}<label for="fault">次の1回の確定処理</label><select name="fault" id="fault"><option value="">通常実行</option><option value="update">更新途中の失敗（E-18）</option><option value="connection">接続中断の模擬（E-19）</option></select><div class="actions"><button type="submit" class="secondary">設定する</button></div></form></section>'
        return self.layout('動作確認',content,'demo')

    def do_GET(self):
        self.session()
        # 同じブラウザの社員切替と確定が並行しないようにする。
        with self.sess['lock']:
            try:
                self.get_route()
            except AppError as error:
                self.send(self.layout('操作できません',self.alert(error)+'<a class="button secondary" href="/">貸出申請へ戻る</a>'),400)
            except sqlite3.DatabaseError:
                self.send('データベースに接続できません。しばらくしてから再度お試しください。 (E-19)',503)

    def get_route(self):
        parts = urlsplit(self.path)
        query = {k:v[-1] for k,v in parse_qs(parts.query).items()}
        if parts.path in ('/static/style.css','/static/app.js'):
            kind = 'text/css; charset=utf-8' if parts.path.endswith('.css') else 'text/javascript; charset=utf-8'
            self.send((ROOT / parts.path.lstrip('/')).read_bytes(),kind=kind)
        elif parts.path == '/':
            values = self.sess.pop('prefill',None)
            self.send(self.lending_page(values))
        elif parts.path == '/confirm':
            self.send(self.confirmation_page(query.get('token','')))
        elif parts.path == '/complete':
            try:
                loan_id = int(query.get('id','0'))
            except ValueError:
                raise AppError('INVALID')
            row = self.server.store.receipt(self.sess['user_id'],loan_id)
            content = f'''<section class="card success"><div class="icon" aria-hidden="true">✓</div><p class="eyebrow">LENDING COMPLETED</p><h1>貸出が完了しました</h1><p>{esc(row['asset_no'])} を貸し出しました。<br>返却予定日は {esc(row['due_date'].replace('-','/'))} です。</p>{self.details(row)}<div class="actions"><a class="button" href="/returns">借りている端末を確認</a><a class="button secondary" href="/">続けて申請する</a></div></section>'''
            self.send(self.layout('貸出完了',content))
        elif parts.path == '/returns':
            self.send(self.returns_page(returned=self.sess.pop('returned',None)))
        elif parts.path == '/demo' and self.server.demo:
            self.send(self.demo_page())
        else:
            self.send('ページが見つかりません。',404)

    def do_POST(self):
        self.session()
        with self.sess['lock']:
            self.post_route()

    def post_route(self):
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 16384:
                self.send('リクエストが大きすぎるか、本文がありません。',413)
                return
            if self.headers.get('Content-Type','').split(';')[0] != 'application/x-www-form-urlencoded':
                self.send('対応していない形式です。',415)
                return
            form = {k:v[-1] for k,v in parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True).items()}
        except (ValueError,UnicodeError):
            self.send('入力形式が正しくありません。',400)
            return
        if not secrets.compare_digest(form.get('csrf',''),self.sess['csrf']):
            self.send('画面の有効期限が切れました。開き直してください。',403)
            return
        path = urlsplit(self.path).path
        try:
            # 操作対象IDは送信値を信用せず、サーバーセッションと照合する。
            if form.get('user_id') != str(self.sess['user_id']):
                raise AppError('E-20')
            uid = self.sess['user_id']
            if path == '/prepare':
                if not form.get('device_id','').isdigit():
                    raise AppError('INVALID')
                token = self.server.store.prepare(uid,int(form['device_id']),form.get('due_date',''),form.get('purpose',''))
                self.redirect('/confirm?'+urlencode({'token':token}))
            elif path == '/lend':
                fault = self.sess.pop('fault',None) if self.server.demo else None
                self.sess['fault'] = None
                loan_id = self.server.store.lend(uid,form.get('token',''),fault)
                self.redirect('/complete?'+urlencode({'id':loan_id}))
            elif path == '/return':
                if not form.get('loan_id','').isdigit():
                    raise AppError('E-16')
                fault = self.sess.pop('fault',None) if self.server.demo else None
                self.sess['fault'] = None
                self.sess['returned'] = self.server.store.return_device(uid,int(form['loan_id']),fault)
                self.redirect('/returns')
            elif path == '/demo/user' and self.server.demo:
                if form.get('employee_id') not in ('1','2','3','4'):
                    raise AppError('INVALID')
                self.sess['user_id'] = int(form['employee_id'])
                self.sess['csrf'] = secrets.token_urlsafe(32)
                self.sess['fault'] = None
                self.sess.pop('prefill',None)
                self.redirect('/demo')
            elif path == '/demo/fault' and self.server.demo:
                if form.get('fault') not in ('','update','connection'):
                    raise AppError('INVALID')
                self.sess['fault'] = form.get('fault') or None
                self.redirect('/demo')
            elif path == '/demo/scenario' and self.server.demo:
                key = form.get('scenario')
                if key not in ('normal','lent','past','failure'):
                    raise AppError('INVALID')
                devices = self.server.store.state(uid)['devices']
                target = next((d for d in devices if d['status']==('LENT' if key=='lent' else 'AVAILABLE')),None)
                if not target:
                    self.send(self.layout('準備できません','<h1>対象の端末がありません</h1><p>貸出・返却で端末の状態を変更してから、もう一度お試しください。</p><a href="/returns">借りている端末へ</a>'),400)
                    return
                self.sess['fault'] = 'update' if key=='failure' else None
                self.sess['prefill'] = {'device_id':str(target['id']),'due_date':(dt.date.today()+dt.timedelta(days=-1 if key=='past' else 7)).isoformat(),'purpose':'社外研修で使用'}
                self.redirect('/')
            else:
                self.send('ページが見つかりません。',404)
        except AppError as error:
            try:
                if path == '/prepare':
                    body = self.lending_page(form,error)
                elif path == '/lend' and error.code in ('E-18','E-19','E-06','E-07','E-08','E-11'):
                    body = self.confirmation_page(form.get('token',''),error)
                elif path == '/return':
                    body = self.returns_page(error)
                else:
                    body = self.layout('操作できません',self.alert(error)+'<a href="/">貸出申請に戻る</a>')
                self.send(body,409 if error.code in ('E-01','E-06','E-07') else 400)
            except (AppError,sqlite3.DatabaseError):
                # DB自体が利用不能でも、二次エラーで応答を失わない。
                self.send(f'<!doctype html><meta charset="utf-8"><h1>{esc(error)}</h1><p>{error.code}</p><a href="/">画面を開き直す</a>',503)
        except sqlite3.DatabaseError:
            self.send('データベースに接続できません。更新は完了していません。 (E-19)',503)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--db',default=str(ROOT/'data'/'gate5.db'))
    parser.add_argument('--demo',action='store_true',help='ローカル検証用の社員切替・障害再現を有効化')
    args = parser.parse_args()
    store = Store(args.db)
    store.initialize()
    server = Server(('127.0.0.1',args.port),store,args.demo)
    print(f'NEXUS Gate5: http://127.0.0.1:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
