"""Print a short-lived TrekOps service JWT — for curl-testing authed routes.

    python scripts/mint_service_token.py viswa
"""

import sys

from app.trekops.api import mint_service_token

if __name__ == "__main__":
    company = sys.argv[1] if len(sys.argv) > 1 else "admin"
    print(mint_service_token(company, ttl_seconds=3600))
