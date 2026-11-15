"""Local-only interactive owner operations. NEVER callable through MCP tools."""
import argparse
import json
from .auth import interactive_login, token_state
from .approvals import approve_interactively, get_plan
from .workspace import account_alias


def main():
    p = argparse.ArgumentParser(description="MSP Bonds local Google OAuth and approvals")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("login", help="Open browser for owner OAuth consent")
    a.add_argument("service", choices=["drive", "gmail", "gmail_write"])
    b = sub.add_parser("approve", help="Explicitly approve one pending cloud change locally")
    b.add_argument("plan_id")
    c = sub.add_parser("show-plan", help="Review a pending plan")
    c.add_argument("plan_id")
    sub.add_parser("status", help="Show token presence only; never token contents")
    args = p.parse_args()
    account = account_alias()
    if args.command == "login":
        print(json.dumps(interactive_login(account, args.service), ensure_ascii=False))
    elif args.command == "approve":
        print(json.dumps(approve_interactively(args.plan_id), ensure_ascii=False))
    elif args.command == "show-plan":
        print(json.dumps(get_plan(args.plan_id), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({s:token_state(account,s) for s in ("drive","gmail","gmail_write")},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
