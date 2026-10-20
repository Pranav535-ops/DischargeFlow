"""Load profiles for the hospital simulation (test plan §4.C), against a seeded sim deployment.

    locust -f sim/locustfile.py --host https://<sim deployment> --headless --csv sim/out/run \
        --html sim/out/run.html            # DF_PROFILE picks the shape (default baseline)

DF_PROFILE      smoke | baseline | shift_change | discharge_peak | soak | spike   (see SHAPES below)
DF_SSE_USERS    number of extra users holding the notification stream open (profile C6), default 0

Virtual users sign in through the dev login as the seeded staff (sim/.state.json), so unit scoping applies exactly
as it would to real staff. Several virtual users may share one staff identity; each has its own session.
Expected refusals (409 prerequisite open, 400 needs a delay code, 403 outside the user's units) are counted as
successes under their own names, so the failure rate means real errors.
"""
from __future__ import annotations

import json
import os
import random
import time

from locust import HttpUser, LoadTestShape, between, events, task

from common import load_state

STATE = load_state()
STAFF = STATE.get("staff", [])
PROFILE = os.environ.get("DF_PROFILE", "baseline")
SSE_USERS = int(os.environ.get("DF_SSE_USERS", "0"))

# (seconds from start, users, spawn rate per second) — the last step's time is the end of the run.
SHAPES = {
    "smoke":          [(90, 20, 5)],  # checks the scripts and the deployment, not a measurement
    "baseline":       [(1800, 50, 2)],
    "shift_change":   [(300, 300, 1), (1500, 300, 1)],
    "discharge_peak": [(120, 200, 2), (3600, 200, 2)],
    "soak":           [(300, 120, 1), (8 * 3600, 120, 1)],
    "spike":          [(120, 50, 5), (150, 400, 20), (600, 400, 20), (720, 50, 20)],
}


class Profile(LoadTestShape):
    def tick(self):
        now = self.get_run_time()
        for until, users, rate in SHAPES[PROFILE]:
            if now < until:
                return users + SSE_USERS, rate
        return None


def staff_of(role: str) -> dict:
    pool = [s for s in STAFF if s["role"] == role]
    return random.choice(pool) if pool else {"handle": "", "units": []}


class Staff(HttpUser):
    abstract = True
    role = "nurse"
    wait_time = between(5, 20)

    def on_start(self):
        self.me = staff_of(self.role)
        self.census_at, self.census = 0.0, []
        self.sign_in()

    def sign_in(self):
        resp = self.client.post("/api/auth/dev-login", json={"role": self.role, "handle": self.me["handle"]},
                                name="auth/dev-login")
        if resp.status_code != 200:
            raise RuntimeError(f"dev login failed ({resp.status_code}); is the sim seeded and dev login on?")
        self.client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    def call(self, method: str, path: str, name: str, ok=(200, 201), expected=(), **kw):
        """A request whose expected refusals count as successes, named `name [status]`."""
        with self.client.request(method, path, name=name, catch_response=True, **kw) as resp:
            if resp.status_code == 401:
                resp.success()
                self.sign_in()
                return None
            if resp.status_code in ok:
                resp.success()
                return resp
            if resp.status_code in expected:
                resp.success()
                self.environment.events.request.fire(request_type="refused", name=f"{name} [{resp.status_code}]",
                                                     response_time=0, response_length=0, exception=None,
                                                     context={})
                return None
            resp.failure(f"{resp.status_code}: {resp.text[:200]}")
            return None

    def my_census(self) -> list[dict]:
        if time.monotonic() - self.census_at > 60:
            resp = self.call("GET", "/api/census", "census")
            if resp is not None:
                self.census, self.census_at = resp.json()["patients"], time.monotonic()
        return self.census

    def complete_one_task(self):
        tasks = [t for row in self.my_census() for t in row["open_tasks"] if t["assigned_role"] == self.role]
        if not tasks:
            return
        t = random.choice(tasks)
        resp = self.call("PUT", f"/api/tasks/{t['task_id']}", "tasks/{id} complete", json={"status": "completed"},
                         expected=(400, 403, 409))
        if resp is None and random.random() < 0.5:   # late task: record why, as the UI asks
            self.call("PUT", f"/api/tasks/{t['task_id']}", "tasks/{id} complete+delay",
                      json={"status": "completed", "delay_code": "documentation"}, expected=(400, 403, 409))
        self.census_at = 0.0


class Nurse(Staff):
    role = "nurse"
    weight = 12

    @task(6)
    def my_tasks(self):
        self.call("GET", "/api/tasks/my", "tasks/my")

    @task(3)
    def census(self):
        self.census_at = 0.0
        self.my_census()

    @task(3)
    def complete(self):
        self.complete_one_task()

    @task(2)
    def notifications(self):
        self.call("GET", "/api/notifications", "notifications")

    @task(1)
    def patient(self):
        rows = self.my_census()
        if rows:
            self.call("GET", f"/api/patients/{random.choice(rows)['patient_id']}", "patients/{id}", expected=(403,))

    @task(1)
    def capacity(self):
        self.call("GET", "/api/capacity/board", "capacity/board")


class Physician(Staff):
    role = "physician"
    weight = 4

    @task(3)
    def plan_review(self):
        resp = self.call("GET", "/api/discharge-plans/review", "discharge-plans/review")
        if resp is None:
            return
        plans = [p for u in resp.json().get("units", []) for p in u.get("plans", []) if not p.get("reviewed_today")]
        if plans:
            enc = random.choice(plans).get("encounter_id")
            if enc:
                self.call("POST", f"/api/encounters/{enc}/plan/review", "encounters/{id}/plan/review",
                          json={"delay_code": "medical"}, expected=(400, 403))

    @task(2)
    def initiate(self):
        rows = [r for r in self.my_census() if r["discharge_status"] == "not_started"]
        if rows:
            self.call("POST", f"/api/workflows/initiate/{random.choice(rows)['patient_id']}", "workflows/initiate",
                      expected=(400, 403))
            self.census_at = 0.0

    @task(2)
    def summary(self):
        rows = [r for r in self.my_census() if r["workflow_id"] and not r["summary_approved"]]
        if not rows:
            return
        row = random.choice(rows)
        resp = self.call("POST", "/api/ai/generate-summary", "ai/generate-summary", expected=(400, 403, 503),
                         json={"patient_id": row["patient_id"], "workflow_id": row["workflow_id"],
                               "clinical_notes": "Improving on oral antibiotics; afebrile 48 h; follow up in 1 week."})
        if resp is not None:
            self.call("PUT", f"/api/ai/approve-summary/{resp.json()['summary_id']}", "ai/approve-summary",
                      expected=(400, 403, 409))

    @task(2)
    def complete_discharge(self):
        rows = [r for r in self.my_census() if r["discharge_status"] == "pending_approval" and r["summary_approved"]]
        if rows:
            self.call("PUT", f"/api/workflows/{random.choice(rows)['workflow_id']}/complete", "workflows/complete",
                      expected=(400, 403))
            self.census_at = 0.0

    @task(2)
    def workflow(self):
        rows = [r for r in self.my_census() if r["workflow_id"]]
        if rows:
            self.call("GET", f"/api/workflows/{random.choice(rows)['workflow_id']}", "workflows/{id}",
                      expected=(403,))

    @task(1)
    def complete(self):
        self.complete_one_task()


class Pharmacist(Staff):
    role = "pharmacist"
    weight = 2

    @task(4)
    def my_tasks(self):
        self.call("GET", "/api/tasks/my", "tasks/my")

    @task(3)
    def complete(self):
        self.complete_one_task()


class Admin(Staff):
    role = "admin"
    weight = 1
    wait_time = between(20, 60)

    @task(2)
    def operations(self):
        self.call("GET", "/api/analytics/operations", "analytics/operations")

    @task(1)
    def outcomes(self):
        self.call("GET", "/api/analytics/outcomes", "analytics/outcomes")

    @task(1)
    def census(self):
        self.call("GET", "/api/census", "census (all units)")


class _StreamHolder(Staff):
    """Holds the notification stream open the way the app does (profile C6), and records time to first event."""
    abstract = True
    role = "nurse"
    wait_time = between(0, 1)

    @task
    def hold_stream(self):
        started = time.perf_counter()
        with self.client.get("/api/notifications/stream", name="notifications/stream", stream=True,
                             catch_response=True, timeout=120) as resp:
            if resp.status_code != 200:
                resp.failure(f"{resp.status_code}")
                if resp.status_code == 401:
                    self.sign_in()
                return
            first = None
            for line in resp.iter_lines():
                if first is None and line:
                    first = (time.perf_counter() - started) * 1000
                    self.environment.events.request.fire(request_type="SSE", name="stream first byte",
                                                         response_time=first, response_length=0, exception=None,
                                                         context={})
                if line and line.startswith(b"event: end"):
                    break
            resp.success()


if SSE_USERS:
    class StreamHolder(_StreamHolder):
        abstract = False
        fixed_count = SSE_USERS  # spawned first, on top of the profile's users


@events.test_stop.add_listener
def verify_audit_chain(environment, **_):
    """After the run: the audit chain must still verify (test plan D3)."""
    try:
        import httpx
        host = environment.host.rstrip("/")
        with httpx.Client(base_url=f"{host}/api", timeout=120) as http:
            token = http.post("/auth/dev-login", json={"role": "admin", "handle": "sim-admi-001"}).json()["token"]
            result = http.get("/audit/verify", headers={"Authorization": f"Bearer {token}"}).json()
        print("audit chain:", json.dumps(result))
        if not result.get("valid"):
            environment.process_exit_code = 1
    except Exception as e:  # report, but don't hide the load results
        print(f"audit chain check failed to run: {e}")
