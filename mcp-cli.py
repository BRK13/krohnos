"""Chamada avulsa ao MCP do hub: python3 mcp-cli.py <ferramenta> [--file args.json] [--json '{"site":"fsite06"}']."""
import argparse
import json
import os
import urllib.request

from hub import config


def _url():
    return os.environ.get("HUB_MCP") or config.ler_hub_json()["mcp"]


def rpc(method, params, request_id=1):
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(_url(), data=payload, headers={"Content-Type": "application/json",
                                                               "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        text = resp.read().decode()
    if text.startswith("event:") or text.startswith("data:"):
        values = [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]
        return next(v for v in values if v.get("id") == request_id)
    return json.loads(text)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('tool')
    ap.add_argument('--file')
    ap.add_argument('--json')
    args = ap.parse_args()
    values = json.load(open(args.file, encoding='utf-8')) if args.file else json.loads(args.json) if args.json else {}
    print(json.dumps(rpc('tools/call', dict(name=args.tool, arguments=values)), ensure_ascii=False))
