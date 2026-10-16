import pytest
"""审查 S6：安全响应头。强制的几条不能影响现有页面，完整 CSP 先 report-only。"""

from fastapi.testclient import TestClient

from tests.test_forum_api import client  # noqa: F401  复用论坛测试的临时库


def _check(response):
    h = response.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "same-origin"
    enforced = h["Content-Security-Policy"]
    assert "object-src 'none'" in enforced and "frame-ancestors 'none'" in enforced
    assert "script-src" not in enforced  # 内联 onclick 还没改掉，不能强制 script-src
    assert "script-src 'self'" in h["Content-Security-Policy-Report-Only"]



def test_security_headers_on_index_api_and_static(client: TestClient):
    _check(client.get("/"))
    _check(client.get("/health"))
    _check(client.get("/api/boards"))
