
import urllib.request
import urllib.error
import json


def test_missing_file_validation():
    url = "http://localhost:5000/api/verify"

    req = urllib.request.Request(
        url,
        data=b"",
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "Expected HTTP 400 when no file is uploaded"

    except urllib.error.HTTPError as e:
        body = e.read().decode()
        data = json.loads(body)

        assert e.code == 400
        assert data.get("error") == "No file uploaded"


def test_unknown_task_id():
    url = "http://localhost:5000/api/verify/invalid_test_task_id"

    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read().decode())

    assert response.status == 200
    assert data.get("status") == "error"
    assert data.get("error") == "Unknown or expired task"
