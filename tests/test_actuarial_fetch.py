import io
import json
import sys
import urllib.error

from benchmarks.actuarial import fetch_sources


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_source_fetch_records_http_failure_and_continues(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "sources.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps({"source_id": "blocked", "title": "Blocked", "landing_url": "https://example.test/blocked"}),
                json.dumps({"source_id": "ok", "title": "OK", "landing_url": "https://example.test/ok"}),
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "downloads"

    def fake_urlopen(request, timeout):
        assert timeout == 120
        if request.full_url.endswith("/blocked"):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO())
        return FakeResponse(b"<html>public actuarial guidance</html>")

    monkeypatch.setattr(fetch_sources.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_sources.py", "--manifest", str(manifest), "--output", str(output)],
    )

    status = fetch_sources.main()
    records = [json.loads(line) for line in (output / ".download_manifest.jsonl").read_text(encoding="utf-8").splitlines()]

    assert status == 2
    assert [record["download_status"] for record in records] == ["error", "ok"]
    assert records[0]["http_status"] == 403
    assert (output / "ok.html").read_bytes() == b"<html>public actuarial guidance</html>"
    assert "failed blocked" in capsys.readouterr().out
