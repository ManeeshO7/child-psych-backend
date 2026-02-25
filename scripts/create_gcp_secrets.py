#!/usr/bin/env python3
"""
Create or update GCP Secret Manager secrets from backend and frontend .env files.
Secrets are named with an environment prefix: dev-SESSION_SECRET, prod-DATABASE_URL, etc.
Usage (from repo root):
  python backend/scripts/create_gcp_secrets.py --project YOUR_GCP_PROJECT --env dev
  python backend/scripts/create_gcp_secrets.py --project YOUR_GCP_PROJECT --env prod
Requires: gcloud CLI installed and authenticated.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file; return dict of KEY -> value (stripped quotes, no comments)."""
    out = {}
    if not path.exists():
        return out
    text = path.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"')
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("\\'", "'")
        out[key] = value
    return out


def run_gcloud(args: list[str]) -> bool:
    """Run gcloud; return True on success."""
    try:
        subprocess.run(
            ["gcloud"] + args,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return False
    except FileNotFoundError:
        print("gcloud CLI not found. Install and log in: gcloud auth login", file=sys.stderr)
        return False


def secret_exists(project: str, secret_name: str) -> bool:
    """Check if a secret exists in the project."""
    result = subprocess.run(
        [
            "gcloud", "secrets", "describe", secret_name,
            "--project", project,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def create_or_update_secret(project: str, env_prefix: str, key: str, value: str) -> bool:
    """Create secret env_prefix-KEY with value, or add new version if it exists."""
    secret_name = f"{env_prefix}-{key}"
    if not value:
        print(f"  Skip {secret_name} (empty value)")
        return True
    # Create secret if it doesn't exist
    if not secret_exists(project, secret_name):
        if not run_gcloud([
            "secrets", "create", secret_name,
            "--project", project,
            "--replication-policy", "automatic",
        ]):
            print(f"  Failed to create secret: {secret_name}", file=sys.stderr)
            return False
    # Add version with the value (stdin)
    proc = subprocess.run(
        [
            "gcloud", "secrets", "versions", "add", secret_name,
            "--project", project,
            "--data-file", "-",
        ],
        input=value,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"  Failed to add version for {secret_name}: {proc.stderr}", file=sys.stderr)
        return False
    print(f"  OK {secret_name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Create GCP Secret Manager secrets from .env files")
    parser.add_argument("--project", required=True, help="GCP project ID")
    parser.add_argument("--env", choices=["dev", "prod"], default="dev", help="Environment prefix for secret names")
    parser.add_argument("--backend-env", default=None, help="Path to backend .env (default: backend/.env from repo root)")
    parser.add_argument("--frontend-env", default=None, help="Path to frontend .env (default: frontend/.env from repo root)")
    parser.add_argument("--repo-root", default=None, help="Repo root (default: parent of backend/)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parent.parent.parent
    backend_env = Path(args.backend_env) if args.backend_env else repo_root / "backend" / ".env"
    frontend_env = Path(args.frontend_env) if args.frontend_env else repo_root / "frontend" / ".env"

    backend_vars = parse_env_file(backend_env)
    frontend_vars = parse_env_file(frontend_env)

    # Merge; frontend can override if same key (unlikely)
    all_vars = {**backend_vars, **frontend_vars}

    # Optional: skip keys that look like local paths or optional
    skip_keys = {"GOOGLE_APPLICATION_CREDENTIALS"}  # path to key file; use workload identity in Cloud Run
    for k in list(all_vars.keys()):
        if k in skip_keys:
            del all_vars[k]

    if not all_vars:
        print("No variables found in .env files.", file=sys.stderr)
        return 1

    print(f"Creating/updating {len(all_vars)} secrets in project {args.project} with prefix {args.env}-")
    ok = 0
    for key, value in sorted(all_vars.items()):
        if create_or_update_secret(args.project, args.env, key, value):
            ok += 1
    print(f"Done: {ok}/{len(all_vars)} secrets.")
    return 0 if ok == len(all_vars) else 1


if __name__ == "__main__":
    sys.exit(main())
