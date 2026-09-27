import datetime as dt
import http.client
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from app import Server
from domain import Store

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'http.db')
        self.store.initialize()
        self.server=Server(('127.0.0.1',0),self.store,True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.cookie=''
        self.csrf=''
        self.request('GET','/')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self,method,path,fields=None):
        con=http.client.HTTPConnection(*self.server.server_address)
        headers={'Cookie':self.cookie}
        body=None
        if fields is not None:
            body=urlencode({'csrf':self.csrf,'user_id':'1',**fields})
            headers['Content-Type']='application/x-www-form-urlencoded'
        con.request(method,path,body,headers)
        res=con.getresponse()
        text=res.read().decode()
        status=res.status
        location=res.getheader('Location')
        if res.getheader('Set-Cookie'):
            self.cookie=res.getheader('Set-Cookie').split(';')[0]
        token=re.search(r'name="csrf" value="([^"]+)"',text)
        if token:self.csrf=token.group(1)
        con.close()
        return status,text,location

    def form(self):
        return dict(device_id='1',due_date=(dt.date.today()+dt.timedelta(days=7)).isoformat(),purpose='研修')

    def test_four_screenshot_states_and_retry(self):
        status,body,_=self.request('POST','/prepare',{**self.form(),'device_id':'2'})
        self.assertEqual(status,409)
        self.assertIn('E-01',body)
        status,body,_=self.request('POST','/prepare',{**self.form(),'due_date':(dt.date.today()-dt.timedelta(days=1)).isoformat()})
        self.assertEqual(status,400)
        self.assertIn('E-08',body)
        self.request('POST','/demo/fault',{'fault':'update'})
        status,_,location=self.request('POST','/prepare',self.form())
        self.assertEqual(status,303)
        status,body,_=self.request('GET',location)
        token=re.search(r'name="token" value="([^"]+)"',body).group(1)
        status,body,_=self.request('POST','/lend',{'token':token})
        self.assertEqual(status,400)
        self.assertIn('E-18',body)
        self.assertEqual(self.store.state(1)['active'],1)
        status,_,location=self.request('POST','/lend',{'token':token})
        self.assertEqual(status,303)
        status,body,_=self.request('GET',location)
        self.assertIn('貸出が完了しました',body)
        self.assertIn('PC-0001 を貸し出しました',body)
        self.assertEqual(self.store.state(1)['inconsistent'],0)

    def test_forged_identity_and_csrf_are_rejected(self):
        status,body,_=self.request('POST','/prepare',{**self.form(),'user_id':'2'})
        self.assertEqual(status,400)
        self.assertIn('E-20',body)
        status,_,_=self.request('POST','/prepare',{**self.form(),'csrf':'wrong'})
        self.assertEqual(status,403)
        self.assertEqual(self.store.state(1)['active'],1)

    def test_client_cannot_replace_confirmed_device_or_purpose(self):
        _,_,location=self.request('POST','/prepare',self.form())
        _,body,_=self.request('GET',location)
        token=re.search(r'name="token" value="([^"]+)"',body).group(1)
        _,_,location=self.request('POST','/lend',{'token':token,'device_id':'3','purpose':'改ざん','due_date':'1990-01-01'})
        _,body,_=self.request('GET',location)
        self.assertIn('PC-0001',body)
        self.assertNotIn('改ざん',body)

    def test_demo_endpoints_disabled_without_flag(self):
        self.server.demo=False
        self.assertEqual(self.request('GET','/demo')[0],404)
        self.assertEqual(self.request('POST','/demo/fault',{'fault':'update'})[0],404)

    def test_output_is_escaped(self):
        _,_,location=self.request('POST','/prepare',{**self.form(),'purpose':'<script>alert(1)</script>'})
        _,body,_=self.request('GET',location)
        self.assertIn('&lt;script&gt;',body)
        self.assertNotIn('<script>alert(1)</script>',body)

if __name__=='__main__':unittest.main()
