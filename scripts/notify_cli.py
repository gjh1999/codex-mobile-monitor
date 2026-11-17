"""Configure credentials in the local process environment; default is dry-run."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from channels_dingtalk import DingTalkWebhook


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--event',default='manual-test')
    parser.add_argument('--title',default='通知链路测试')
    parser.add_argument('--body',default='Codex 钉钉通知配置检查')
    parser.add_argument('--send',action='store_true')
    args=parser.parse_args()
    webhook=os.environ.get('DINGTALK_WEBHOOK')
    secret=os.environ.get('DINGTALK_SIGNING_SECRET')
    if not webhook or not secret:
        print(json.dumps({'status':'not_configured','channel':'dingtalk',
            'required_env':['DINGTALK_WEBHOOK','DINGTALK_SIGNING_SECRET']},ensure_ascii=False))
        return
    channel=DingTalkWebhook(webhook,secret)
    if not args.send:
        print(json.dumps({'status':'configured_dry_run','channel':'dingtalk','network_request':False}))
        return
    print(json.dumps(channel.send(args.event,args.title,args.body)))


if __name__=='__main__':main()
