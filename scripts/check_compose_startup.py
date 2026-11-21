"""Exercise the shipped Compose startup flow using disposable Linux projects.

Run against a locally built application image. Each case owns its generated
project, network and volumes; no deployment .env or host data is mounted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
COMPOSE = PROJECT / "docker-compose.yaml"
CONFIG_KEYS = {
    "APP_IMAGE",
    "POSTGRES_IMAGE",
    "STORAGE_ROOT",
    "BACKUP_ROOT",
    "RESTORE_ROOT",
    "HTTP_PORT",
    "HTTP_BIND_HOST",
    "ALLOWED_ORIGINS",
    "TUSHARE_TOKEN",
    "TUSHARE_HTTP_URL",
}


def execute(arguments, *, input_text=None, check=True, timeout=240):
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in CONFIG_KEYS
        and not name.startswith(("COMPOSE_", "POSTGRES_", "RESTORE_POSTGRES_", "FINANCE_QUANT_"))
    }
    result = subprocess.run(
        arguments, input=input_text, capture_output=True, text=True, env=environment, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"Command failed: {' '.join(arguments)}\n{result.stdout}\n{result.stderr}")
    return result


HEAD_REVISION = "0002_forward_registration"


class Stack:
    def __init__(self, directory, image, label):
        self.root = directory / label
        self.root.mkdir()
        self.project = "fq-startup-" + uuid.uuid4().hex[:12]
        self.image = image
        self.base_image = image
        self.password = "isolated-$value # @:/ " + chr(92) + chr(34) + chr(39)
        self.override = None
        self.configure()
        config = json.loads(self.run("config", "--format", "json").stdout)
        if config["name"] != self.project:
            raise ValueError("Refusing to use a non-isolated Compose project")
        default_command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "--env-file",
            str(self.root / ".env"),
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ]
        default_config = json.loads(execute(default_command).stdout)
        assert default_config["name"] == self.root.name, "Compose project name does not follow its deployment folder"
        for definition in config.get("volumes", {}).values():
            if definition.get("external") or not definition["name"].startswith(self.project + "_"):
                raise ValueError("Refusing to use a non-isolated volume")
        for service in config["services"].values():
            for mount in service.get("volumes", []):
                if mount["type"] == "bind" and not Path(mount["source"]).resolve().is_relative_to(self.root.resolve()):
                    raise ValueError("Startup tests cannot mount existing host data")

    def configure(self):
        values = {
            "APP_IMAGE": self.image,
            "POSTGRES_USER": "fq_startup",
            "POSTGRES_DB": "fq_startup_test",
            "POSTGRES_PASSWORD": self.password,
            "POSTGRES_DATA_ROOT": str(self.root / "postgres"),
            "TUSHARE_TOKEN": "",
            "HTTP_PORT": "0",
            "HTTP_BIND_HOST": "127.0.0.1",
            "STORAGE_ROOT": str(self.root / "storage"),
            "BACKUP_ROOT": str(self.root / "backups"),
            "RESTORE_ROOT": str(self.root / "restores"),
        }
        lines = [key + "='" + value.replace("'", "\\'") + "'" for key, value in values.items()]
        (self.root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_override(self, services):
        self.override = self.root / "override.json" if services else None
        if services:
            self.override.write_text(json.dumps({"services": services}), encoding="utf-8")

    def run(self, *arguments, **options):
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(self.root),
            "--env-file",
            str(self.root / ".env"),
            "-f",
            str(COMPOSE),
        ]
        if self.override:
            command += ["-f", str(self.override)]
        return execute([*command, *arguments], **options)

    def states(self):
        raw = self.run("ps", "--all", "--format", "json").stdout.strip()
        rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
        return {row["Service"]: row for row in rows}

    def ready(self, revision=HEAD_REVISION):
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            states = self.states()
            if states.get("api", {}).get("Health") == "healthy" and states.get("worker", {}).get("State") == "running":
                break
            time.sleep(1)
        else:
            raise AssertionError("Application services did not become ready")
        assert states["db"]["Health"] == "healthy", states
        assert states["storage-init"]["State"] == "exited" and states["storage-init"]["ExitCode"] == 0, states
        assert states["migrate"]["State"] == "exited" and states["migrate"]["ExitCode"] == 0, states
        for service in ("db", "migrate", "api", "worker"):
            identifier = self.run("ps", "--all", "-q", service).stdout.strip()
            container = json.loads(execute(["docker", "inspect", identifier]).stdout)[0]
            environment = dict(item.split("=", 1) for item in container["Config"]["Env"])
            assert environment["POSTGRES_PASSWORD"] == self.password, service + " changed the literal password"
            if service in {"api", "worker"}:
                assert container["RestartCount"] == 0, service + " restarted during startup"
        endpoint = self.run("port", "api", "8000").stdout.strip()
        with urllib.request.urlopen("http://" + endpoint + "/api/v1/health", timeout=5) as response:
            assert json.load(response)["status"] == "ok"
        assert self.sql("SELECT version_num FROM alembic_version;") == revision

    def sql(self, query):
        command = 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -w -v ON_ERROR_STOP=1 -At -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
        return self.run("exec", "-T", "db", "sh", "-c", command, input_text=query).stdout.strip()

    def stopped_apps(self):
        states = self.states()
        for service in ("api", "worker"):
            assert states.get(service, {}).get("State") not in {"running", "restarting"}, states

    def close(self):
        self.run("down", "--volumes", "--remove-orphans", check=False)
        # Database and app files have different image UIDs. Remove only this
        # case's four validated temporary directories after stopping containers.
        cleanup = "from pathlib import Path; import shutil; [shutil.rmtree(p) for p in Path('/cleanup').iterdir() if p.name in {'postgres', 'storage', 'backups', 'restores'}]"
        execute(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--entrypoint",
                "python",
                "--volume",
                str(self.root) + ":/cleanup",
                self.base_image,
                "-c",
                cleanup,
            ]
        )


def upgrade_image(directory, image):
    root = directory / "upgrade"
    root.mkdir()
    (root / "9000_startup_probe.py").write_text(
        """from alembic import op
import sqlalchemy as sa

revision = "startup_probe_v2"
down_revision = HEAD_REVISION
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("compose_upgrade_probe", sa.Column("id", sa.Integer(), primary_key=True))

def downgrade():
    op.drop_table("compose_upgrade_probe")
""",
        encoding="utf-8",
    )
    (root / "Dockerfile").write_text(
        """ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY 9000_startup_probe.py /app/alembic/versions/
RUN python -c "from pathlib import Path; p = Path('/app/src/finance_quant/persistence/database.py'); p.write_text(p.read_text().replace('0002_forward_registration', 'startup_probe_v2'))"
USER quant
""",
        encoding="utf-8",
    )
    tag = "finance-quant-startup-upgrade:" + uuid.uuid4().hex[:12]
    execute(["docker", "build", "--build-arg", "BASE_IMAGE=" + image, "-t", tag, str(root)])
    return tag


def startup_and_upgrade(stack, directory):
    print("Checking fresh startup, password literals and writable volumes...", flush=True)
    stack.run("up", "-d")
    stack.ready()
    stack.sql(
        "CREATE TABLE compose_startup_probe (value text NOT NULL); INSERT INTO compose_startup_probe VALUES ('kept');"
    )
    probe = "from pathlib import Path; import os; assert os.getuid() != 0; "
    probe += "[Path(p, 'startup-probe').write_text('kept') for p in ['/app/storage', '/backups', '/restores']]"
    stack.run("exec", "-T", "api", "python", "-c", probe)
    print("Checking repeated compose up preserves schema and data...", flush=True)
    stack.run("up", "-d")
    stack.ready()
    assert stack.sql("SELECT value FROM compose_startup_probe;") == "kept"
    upgraded = upgrade_image(directory, stack.image)
    try:
        print("Checking image update applies a pending migration before startup...", flush=True)
        stack.run("stop", "-t", "30", "worker", "api")
        stack.image = upgraded
        stack.configure()
        stack.run("up", "-d")
        stack.ready("startup_probe_v2")
        assert stack.sql("SELECT to_regclass('compose_upgrade_probe') IS NOT NULL;") == "t"
        assert stack.sql("SELECT value FROM compose_startup_probe;") == "kept"
        probe = "from pathlib import Path; assert all(Path(p, 'startup-probe').read_text() == 'kept' for p in ['/app/storage', '/backups', '/restores'])"
        stack.run("exec", "-T", "api", "python", "-c", probe)
    finally:
        stack.close()
        execute(["docker", "image", "rm", upgraded], check=False)


def migration_failure(stack):
    print("Checking maintenance prepares writable directories without migrating an empty database...", flush=True)
    probe = "from pathlib import Path; import os; assert os.getuid() != 0; "
    probe += "[Path(p, 'maintenance-probe').write_text('ready') for p in ['/app/storage', '/backups', '/restores']]"
    stack.run("run", "--rm", "-T", "maintenance", "python", "-c", probe)
    assert stack.sql("SELECT to_regclass('alembic_version') IS NULL;") == "t"
    assert "migrate" not in stack.states()
    stack.stopped_apps()
    print("Checking failed migrations block API and worker, then recover...", flush=True)
    stack.set_override({"migrate": {"command": ["python", "-c", "raise SystemExit(23)"]}})
    assert stack.run("up", "-d", check=False).returncode != 0
    stack.stopped_apps()
    assert stack.states()["migrate"]["ExitCode"] == 23
    stack.set_override(None)
    stack.run("up", "-d")
    stack.ready()


def changed_password(stack):
    print("Checking existing-volume password mismatch blocks startup; fresh initialization succeeds...", flush=True)
    stack.run("up", "-d", "--wait", "db")
    stack.password = "different-isolated-password"
    stack.configure()
    stack.set_override({"db": {"healthcheck": {"interval": "1s", "retries": 3, "start_period": "0s"}}})
    assert stack.run("up", "-d", check=False).returncode != 0
    assert stack.states()["db"]["Health"] == "unhealthy", "Invalid credentials passed the database health check"
    assert stack.states().get("migrate", {}).get("State") in {None, "created"}, (
        "Migration started before database authentication passed"
    )
    stack.stopped_apps()
    stack.close()
    stack.set_override(None)
    stack.run("up", "-d")
    stack.ready()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally available application image to test")
    options = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run Compose startup checks on Linux")
    stacks = []
    with tempfile.TemporaryDirectory(prefix="finance-quant-compose-") as temporary:
        directory = Path(temporary)
        try:
            startup = Stack(directory, options.image, "startup")
            stacks.append(startup)
            startup_and_upgrade(startup, directory)
            failure = Stack(directory, options.image, "migration-failure")
            stacks.append(failure)
            migration_failure(failure)
            failure.close()
            credentials = Stack(directory, options.image, "credentials")
            stacks.append(credentials)
            changed_password(credentials)
            print("Compose startup checks passed.", flush=True)
        except BaseException:
            for stack in stacks:
                print(stack.run("logs", "--no-color", "--tail", "60", check=False).stdout, flush=True)
            raise
        finally:
            for stack in reversed(stacks):
                stack.close()


if __name__ == "__main__":
    main()
