import asyncio
import importlib.util
import unittest
from unittest.mock import patch

@unittest.skipUnless(importlib.util.find_spec('dingtalk_stream'), 'Optional Stream SDK not installed')
class SDKTests(unittest.TestCase):
    def test_real_sdk_registration_and_callback_without_network(self):
        import dingtalk_stream
        from dingtalk_commands import run_stream
        clients=[]
        class Router:
            def handle(self,data):
                self.data=data
                return 'offline reply'
        router=Router()
        def capture(client):
            clients.append(client)
        with patch.object(dingtalk_stream.DingTalkStreamClient,'start_forever',capture):
            run_stream(router,'offline-client','offline-secret')
        client=clients[0]
        handler=client.callback_handler_map[dingtalk_stream.ChatbotMessage.TOPIC]
        class Callback:
            data={'msgtype':'text','text':{'content':'状态'}}
        with patch.object(handler,'reply_text') as reply:
            result=asyncio.run(handler.process(Callback()))
            reply.assert_called_once()
        self.assertEqual(result[0],dingtalk_stream.AckMessage.STATUS_OK)
        self.assertEqual(router.data,Callback.data)
