import os
import sys
from unittest.mock import patch

from finance_quant import server
from finance_quant.persistence import database


def test_native_api_loads_development_env_and_passes_uvicorn_options(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.development"
    env_file.write_text(
        "POSTGRES_HOST=127.0.0.1\n"
        "POSTGRES_DB=finance_quant_dev\n"
        "POSTGRES_USER=quant_dev\n"
        "POSTGRES_PASSWORD=development-only\n"
        "HTTP_HOST=127.0.0.2\n"
        "HTTP_PORT=8123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(database, "check_schema", lambda: None)
    monkeypatch.setattr(server, "paths", lambda: (tmp_path, tmp_path / "runs", tmp_path / "data"))
    captured = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **options):
            captured.update(app=app, **options)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    monkeypatch.setattr(
        sys,
        "argv",
        ["finance-quant-server", "api", "--env-file", str(env_file), "--reload"],
    )

    with patch.dict(os.environ, {}, clear=False):
        for name in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "HTTP_HOST", "HTTP_PORT"):
            os.environ.pop(name, None)
        server.main()

    assert captured == {
        "app": "finance_quant.server:create_http_app",
        "factory": True,
        "host": "127.0.0.2",
        "port": 8123,
        "reload": True,
    }


def test_missing_development_env_is_rejected_before_database_access(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.env"
    monkeypatch.setattr(sys, "argv", ["finance-quant-server", "check", "--env-file", str(missing)])

    try:
        server.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("missing env file must stop startup")

    assert "env file does not exist" in capsys.readouterr().err
