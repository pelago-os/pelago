# Historical reference only. Not integrated or supported for deployment. See README.md.
import json
import urllib.request
import urllib.error
import urllib.parse
import socket
import ssl
from typing import Dict, Any, Optional

class CoordinatorClient:
    def __init__(self, base_url: str, token: str, timeout: int = 10, tls_verify: bool = True):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.tls_verify = tls_verify
        if not tls_verify:
            self.ssl_context = ssl._create_unverified_context()
        else:
            self.ssl_context = ssl.create_default_context()

    def _post(self, path: str, data: Dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode('utf-8'),
            headers={
                "X-Example-Worker-Protocol": "1",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as response:
                if response.status == 204:
                    return None
                return json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, socket.timeout) as e:
            if isinstance(e, urllib.error.HTTPError):
                err_body = e.read().decode('utf-8')
                raise RuntimeError(f"HTTP {e.code}: {err_body}")
            raise RuntimeError(f"Network Error: {str(e)}")

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "X-Example-Worker-Protocol": "1",
                "Authorization": f"Bearer {self.token}"
            },
            method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as response:
                if response.status == 204:
                    return None
                return json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, socket.timeout) as e:
            if isinstance(e, urllib.error.HTTPError):
                err_body = e.read().decode('utf-8')
                raise RuntimeError(f"HTTP {e.code}: {err_body}")
            raise RuntimeError(f"Network Error: {str(e)}")

    def get_windows(self, instance_id: str) -> Dict[str, Any]:
        encoded_id = urllib.parse.quote(instance_id)
        return self._get(f"/api/meeting-room/workers/windows?instance_id={encoded_id}")

    def register(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/api/meeting-room/workers/register", data)

    def heartbeat(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/api/meeting-room/workers/heartbeat", data)

    def lease(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/api/meeting-room/workers/lease", data)

    def task_started(self, task_id: str, data: Dict[str, Any]) -> None:
        self._post(f"/api/meeting-room/workers/tasks/{task_id}/started", data)

    def task_complete(self, task_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/api/meeting-room/workers/tasks/{task_id}/complete", data)

    def task_fail(self, task_id: str, data: Dict[str, Any]) -> None:
        self._post(f"/api/meeting-room/workers/tasks/{task_id}/fail", data)
