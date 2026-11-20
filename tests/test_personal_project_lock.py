import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from coordination_server import create_app


class ProjectLockTests(unittest.TestCase):
    def test_devices_race_by_repository_and_ide_reports_share_the_lock(self):
        devices = {device: {"token": device * 43, "personalSpace": space, "projects": []}
                   for device, space in (("a", "one"), ("b", "one"), ("c", "two"))}
        headers = lambda device: {"X-Lanxi-Device": device, "Authorization": "Bearer " + device * 43}
        repo = "company/example"
        first, second, ide = (str(uuid4()) for _ in range(3))
        routes = "/api/v1/personal"
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "server.sqlite3"
            with TestClient(create_app(database, devices, personal_only=True)) as api:
                def claim(device, session, checkpoint=None):
                    return api.post(f"{routes}/tasks/{session}/claim", headers=headers(device),
                                    json={"project": repo, "checkpoint": checkpoint})
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a, b = [future.result() for future in (pool.submit(claim, "a", first), pool.submit(claim, "b", second))]
                self.assertEqual(sorted((a.status_code, b.status_code)), [200, 409])
                owner, blocked, active, waiting, ticket = (
                    ("a", "b", first, second, a.json()["ticket"]) if a.status_code == 200
                    else ("b", "a", second, first, b.json()["ticket"])
                )
                self.assertEqual(claim("c", waiting).status_code, 200)
                self.assertEqual(api.get(f"{routes}/tasks", headers=headers(blocked)).json()["projects"], {repo: owner})
                activity = f"{routes}/project-activity"
                self.assertEqual(api.post(activity, headers=headers(blocked),
                    json={"project": repo, "session": ide, "event": "open"}).status_code, 409)
                handoff = f"{routes}/tasks/{active}/handoff"
                release = {"ticket": ticket, "checkpoint": "1" * 64}
                self.assertEqual(api.post(handoff, headers=headers(blocked), json=release).status_code, 409)
                self.assertEqual(api.post(handoff, headers=headers(owner), json=release).status_code, 200)
                self.assertEqual(claim(blocked, active).status_code, 409)
                self.assertEqual(api.post(activity, headers=headers(blocked),
                    json={"project": repo, "session": ide, "event": "open"}).status_code, 200)
                self.assertEqual(claim(owner, active, "1" * 64).status_code, 409)
                self.assertEqual(api.post(activity, headers=headers(blocked),
                    json={"project": repo, "session": ide, "event": "close"}).status_code, 200)
                self.assertEqual(claim(blocked, active, "1" * 64).status_code, 200)
            with TestClient(create_app(database, devices, personal_only=True)) as restarted:
                self.assertEqual(restarted.get(f"{routes}/tasks", headers=headers(owner)).json()["projects"],
                                 {repo: blocked})
