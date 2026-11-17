"""DingTalk signed text webhook. No outbound call until explicitly configured.

Transport is injectable for offline verification. Secrets never enter exceptions.
"""
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import urllib.request


class DingTalkWebhook:
    def __init__(self,webhook,secret,*,transport=urllib.request.urlopen,sleep=time.sleep,clock=time.time):
        parsed=urllib.parse.urlsplit(webhook)
        query=urllib.parse.parse_qs(parsed.query)
        if (parsed.scheme!='https' or parsed.hostname!='oapi.dingtalk.com' or
            parsed.path!='/robot/send' or parsed.username or parsed.password or
            parsed.fragment or parsed.port not in (None,443) or len(query.get('access_token',[]))!=1 or
            not query['access_token'][0] or not secret):
            raise ValueError('Invalid DingTalk webhook configuration')
        self._base=urllib.parse.urlunsplit(('https','oapi.dingtalk.com','/robot/send',
            urllib.parse.urlencode({'access_token':query['access_token'][0]}),''))
        self._secret=secret
        self._transport,self._sleep,self._clock=transport,sleep,clock
        self._sent=set()
        self._lock=threading.Lock()

    def __repr__(self):
        return 'DingTalkWebhook(configured=True, credentials=REDACTED)'

    def _request(self,text):
        timestamp=str(int(self._clock()*1000))
        message=f'{timestamp}\n{self._secret}'.encode()
        sign=base64.b64encode(hmac.new(self._secret.encode(),message,hashlib.sha256).digest()).decode()
        url=self._base+'&'+urllib.parse.urlencode({'timestamp':timestamp,'sign':sign})
        payload=json.dumps({'msgtype':'text','text':{'content':text},'at':{'isAtAll':False}},ensure_ascii=False).encode()
        return urllib.request.Request(url,data=payload,headers={'Content-Type':'application/json'},method='POST')

    def send(self,event_id,title,body):
        if not event_id or len(title)>200 or len(body)>3000:
            raise ValueError('Invalid notification event')
        # Serializing delivery prevents concurrent duplicate sends in one process.
        with self._lock:
            if event_id in self._sent:
                return {'status':'duplicate','attempts':0}
            for attempt in range(3):
                try:
                    with self._transport(self._request(f'Codex｜{title}\n{body}'),timeout=10) as response:
                        result=json.loads(response.read(65536))
                    if type(result.get('errcode')) is not int or result['errcode']!=0:
                        # Retry only the explicit throttling code; auth/policy errors need configuration.
                        if result.get('errcode')==130101 and attempt<2:
                            self._sleep(2**attempt)
                            continue
                        return {'status':'rejected','attempts':attempt+1}
                    self._sent.add(event_id)
                    return {'status':'sent','attempts':attempt+1}
                except Exception:
                    # Network outcome may be uncertain: provider has no idempotency key.
                    # Do not retry a possibly delivered POST and spam the recipient.
                    return {'status':'unknown_delivery','attempts':attempt+1}
            return {'status':'rejected','attempts':3}
