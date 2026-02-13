#!/usr/bin/env python3
"""
Disable Resend click tracking for your domain so email links go directly to the
destination (e.g. localhost) instead of through resend-clicks.com.

Resend only allows click tracking to be configured at the domain level, not per-email.
The "click_tracking": False in Emails.send() is ignored.

Usage:
  # List your domains and their IDs first:
  RESEND_API_KEY=re_xxx .venv/bin/python scripts/disable_resend_click_tracking.py list

  # Disable click tracking for a domain (use the ID from list):
  RESEND_API_KEY=re_xxx .venv/bin/python scripts/disable_resend_click_tracking.py <domain_id>

  # Or set domain ID in env:
  RESEND_API_KEY=re_xxx RESEND_DOMAIN_ID=xxx .venv/bin/python scripts/disable_resend_click_tracking.py
"""
import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from dotenv import load_dotenv
load_dotenv()


def main() -> None:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key:
        print("Error: Set RESEND_API_KEY in .env or environment.")
        sys.exit(1)

    import resend
    resend.api_key = api_key

    if len(sys.argv) > 1 and sys.argv[1].lower() == "list":
        domains = resend.Domains.list()
        items = domains.get("data", []) if isinstance(domains, dict) else []
        if not items:
            print("No domains found. Add and verify a domain in https://resend.com/domains")
            return
        print("Domains (use the 'id' to disable click tracking):\n")
        for d in items:
            dom_id = getattr(d, "id", None) or d.get("id", "?")
            name = getattr(d, "name", None) or d.get("name", "?")
            status = getattr(d, "status", None) or d.get("status", "?")
            print(f"  id: {dom_id}  name: {name}  status: {status}")
        return

    domain_id = (os.environ.get("RESEND_DOMAIN_ID") or "").strip()
    if len(sys.argv) > 1 and sys.argv[1] != "list":
        domain_id = sys.argv[1].strip()
    if not domain_id:
        print("Usage: python scripts/disable_resend_click_tracking.py list")
        print("       python scripts/disable_resend_click_tracking.py <domain_id>")
        print("Or set RESEND_DOMAIN_ID in env.")
        sys.exit(1)

    resend.Domains.update(
        {"id": domain_id, "click_tracking": False, "open_tracking": False}
    )
    print("Click tracking disabled for domain.")
    print("New emails will use direct links instead of resend-clicks.com.")


if __name__ == "__main__":
    main()
