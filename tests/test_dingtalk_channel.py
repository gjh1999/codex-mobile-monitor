import base64,hashlib,hmac,json,unittest,urllib.parse
from channels_dingtalk import DingTalkWebhook


class Response:
    def __init__(self,value):self.value=value
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,*args):return json.dumps(self.value).encode()


class DingTalkTests(unittest.TestCase):
    def make(self,transport):
        return DingTalkWebhook('https://oapi.dingtalk.com/robot/send?access_token=TEST_ONLY',
            'TEST_SECRET',transport=transport,sleep=lambda n:None,clock=lambda:1234)

    def test_signature_payload_and_no_duplicate(self):
        calls=[]
        def transport(req,timeout):
            calls.append(req)
            return Response({'errcode':0})
        channel=self.make(transport)
        self.assertEqual(channel.send('e1','训练完成','5000步')['status'],'sent')
        self.assertEqual(channel.send('e1','训练完成','5000步')['status'],'duplicate')
        self.assertEqual(len(calls),1)
        request=calls[0]
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        expected=base64.b64encode(hmac.new(b'TEST_SECRET',b'1234000\nTEST_SECRET',hashlib.sha256).digest()).decode()
        self.assertEqual(query['sign'],[expected])
        self.assertEqual(json.loads(request.data)['msgtype'],'text')
        self.assertNotIn('TEST_SECRET',repr(channel))

    def test_business_rejection_not_http_success(self):
        channel=self.make(lambda *a,**k:Response({'errcode':310000,'errmsg':'policy rejected'}))
        self.assertEqual(channel.send('e2','状态','等待')['status'],'rejected')

    def test_network_uncertainty_not_retried(self):
        calls=[]
        def fail(*a,**k):
            calls.append(1)
            raise TimeoutError('secret-bearing URL must not be returned')
        result=self.make(fail).send('e3','状态','等待')
        self.assertEqual(result,{'status':'unknown_delivery','attempts':1})
        self.assertEqual(len(calls),1)

    def test_wrong_destination_rejected(self):
        with self.assertRaises(ValueError):
            DingTalkWebhook('https://example.org/robot/send?access_token=x','secret')


if __name__=='__main__':unittest.main()
