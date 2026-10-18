"""Post-deploy smoke test: python scripts/smoke_test.py https://app.example.org

Unauthenticated checks only (no test accounts in deployed environments): the SPA and API are served from the same
origin, the API reaches its database, authentication is enforced, and the security headers are present.
Exits non-zero on the first failure. Standard library only, so it runs on any CI runner.
"""
import json
import sys
import time
import urllib.error
import urllib.request


def fetch(url: str, method: str = "GET") -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "dischargeflow-smoke"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        sys.exit(1)


def main(base: str) -> None:
    base = base.rstrip("/")
    # A fresh rollout can take a few seconds to reach every edge location.
    for attempt in range(5):
        status, headers, body = fetch(f"{base}/api/health")
        if status == 200:
            break
        time.sleep(5)
    check("GET /api/health is 200", status == 200, f"got {status}")
    health = json.loads(body)
    check("API reports a healthy PostgreSQL database",
          health.get("status") == "healthy" and health.get("database") == "postgresql", str(health))
    check("responses carry X-Request-ID", bool(headers.get("x-request-id")))
    check("HSTS header present", "max-age" in headers.get("strict-transport-security", ""))
    check("nosniff header present", headers.get("x-content-type-options") == "nosniff")

    status, _, _ = fetch(f"{base}/api/patients")
    check("patient data requires authentication", status == 401, f"got {status}")
    status, _, _ = fetch(f"{base}/api/auth/refresh", method="POST")
    check("refresh without a cookie is refused", status == 401, f"got {status}")

    status, headers, body = fetch(f"{base}/dashboard")
    check("SPA route serves the React app", status == 200 and b'id="root"' in body, f"got {status}")
    print(f"Smoke test passed for {base} (API version {health.get('version')})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
