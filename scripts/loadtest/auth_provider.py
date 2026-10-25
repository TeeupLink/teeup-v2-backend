from __future__ import annotations

from datetime import datetime
from threading import Lock

from state_store import UserRuntimeState


class AuthProvider:
    _bootstrap_lock = Lock()
    _bootstrap_done = False

    def __init__(self, client, run_id: str):
        self.client = client
        self.run_id = run_id

    @staticmethod
    def _headers(token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def ensure_bootstrap(self) -> bool:
        if AuthProvider._bootstrap_done:
            return True

        with AuthProvider._bootstrap_lock:
            if AuthProvider._bootstrap_done:
                return True

            with self.client.post(
                "/api/v1/internal/loadtest/bootstrap",
                headers=self._headers(),
                name="internal.loadtest_bootstrap",
                catch_response=True,
            ) as response:
                if response.status_code != 200:
                    response.failure(f"bootstrap failed: {response.status_code} {response.text}")
                    return False
                response.success()
                AuthProvider._bootstrap_done = True
                return True

    def oauth_mock_login(self, state: UserRuntimeState) -> bool:
        payload = {
            "provider_id": f"load-{state.run_id}-{state.vu_id}",
            "email": f"load+{state.run_id}-{state.vu_id}@teeup.run",
            "name": f"load_{state.vu_id}",
            "verified_email": True,
        }
        with self.client.post(
            "/api/v1/internal/loadtest/oauth-mock",
            json=payload,
            headers=self._headers(),
            name="auth.oauth_mock",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"oauth mock failed: {response.status_code} {response.text}")
                return False
            data = response.json()
            state.access_token = data.get("access_token")
            state.refresh_token = data.get("refresh_token")
            user = data.get("user") or {}
            state.user_id = user.get("id")
            state.email = user.get("email")
            state.is_new_user = bool(data.get("is_new_user"))
            response.success()
            return True

    def agree_required_terms(self, state: UserRuntimeState) -> bool:
        with self.client.get(
            "/api/v1/terms/active/",
            headers=self._headers(state.access_token),
            name="terms.active",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"terms list failed: {response.status_code} {response.text}")
                return False
            terms = response.json() or []
            required_ids = [term["id"] for term in terms if term.get("is_required")]
            response.success()

        if not required_ids:
            return True

        payload = {
            "terms_ids": required_ids,
            "agreed_at": datetime.utcnow().isoformat(),
            "ip_address": "127.0.0.1",
            "user_agent": "loadtest/locust",
        }
        with self.client.post(
            "/api/v1/terms/agreements/bulk",
            json=payload,
            headers=self._headers(state.access_token),
            name="terms.agree_bulk",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"terms bulk failed: {response.status_code} {response.text}")
                return False
            response.success()
            return True

    def complete_profile(self, state: UserRuntimeState) -> bool:
        payload = {
            "realname": f"Load User {state.vu_id}",
            "phone_number": f"0100000{state.vu_id:04d}"[-11:],
            "birthdate": "1990-01-01",
            "gender": "MALE" if state.vu_id % 2 == 0 else "FEMALE",
            "average_score_init": 95,
        }
        with self.client.put(
            "/api/v1/users/me",
            json=payload,
            headers=self._headers(state.access_token),
            name="users.complete_profile",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"profile update failed: {response.status_code} {response.text}")
                return False
            response.success()
            return True
