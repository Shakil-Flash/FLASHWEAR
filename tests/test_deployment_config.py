"""Phase 18: deployment artefacts no HTTP request can ever reach.

The Dockerfile, the compose files, the proxy config and the gunicorn settings are
ordinary production code -- they just happen to be read by Docker rather than by
Django. A healthcheck pointing at a route that does not exist, a build that skips
collectstatic (and therefore boots a WhiteNoise manifest with nothing in it), or a
compose stack that silently drops beat are exactly the regressions a request-based
suite cannot see, so each is pinned here as text.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml
from django.urls import resolve

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestDockerignore:
    def test_secrets_environments_and_artefacts_never_reach_the_build_context(self):
        ignored = {
            line.strip()
            for line in _read(".dockerignore").splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert {
            ".env",
            ".venv",
            ".git",
            "node_modules",
            "staticfiles",
            "media",
            "logs",
            "tests",
        } <= ignored

    def test_the_build_still_gets_the_files_it_copies(self):
        # Dockerfile does `COPY pyproject.toml README.md ./` before the source tree.
        ignored = {
            line.strip()
            for line in _read(".dockerignore").splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert "README.md" not in ignored
        assert "!README.md" in ignored  # negated after the blanket `*.md`


class TestImage:
    def test_static_assets_are_baked_in_at_build_time(self):
        dockerfile = _read("Dockerfile")
        assert "collectstatic --noinput" in dockerfile
        # Under production settings: a build that collected against development
        # settings would happily produce a bundle production then refuses to serve.
        assert "DJANGO_SETTINGS_MODULE=config.settings.production" in dockerfile

    def test_the_healthcheck_calls_a_route_that_exists(self):
        import re

        dockerfile = _read("Dockerfile")
        match = re.search(r"http://127\.0\.0\.1:8000(\S+)", dockerfile)
        assert match, "no healthcheck URL found in the Dockerfile"
        # resolve() raises Resolver404 for a route nobody registered.
        resolve(match.group(1))

    def test_the_healthcheck_survives_the_ssl_redirect(self):
        # Without X-Forwarded-Proto: https, SECURE_SSL_REDIRECT answers 301 to the
        # probe and the check passes without ever reaching the view.
        assert 'curl -fsS -H "X-Forwarded-Proto: https"' in _read("Dockerfile")


class TestGunicornConfig:
    def _load(self, monkeypatch, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        path = ROOT / "gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_defaults_are_sane_and_the_port_matches_the_image(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        config = self._load(monkeypatch)
        assert config.bind == "0.0.0.0:8000"
        assert config.workers >= 1
        assert config.timeout > 0

    def test_workers_and_timeouts_come_from_the_environment(self, monkeypatch):
        config = self._load(monkeypatch, WEB_CONCURRENCY="5", GUNICORN_TIMEOUT="45", PORT="9001")
        assert config.workers == 5
        assert config.timeout == 45
        assert config.bind == "0.0.0.0:9001"


class TestComposeProd:
    def _services(self) -> dict:
        return yaml.safe_load(_read("docker-compose.prod.yml"))["services"]

    def test_the_stack_runs_web_worker_beat_and_nginx(self):
        services = self._services()
        assert {"db", "redis", "web", "worker", "beat", "nginx"} <= set(services)
        assert "celery -A config beat" in services["beat"]["command"]
        assert "celery -A config worker" in services["worker"]["command"]

    def test_the_database_and_cache_are_not_published_to_the_host(self):
        for name in ("db", "redis"):
            assert "ports" not in self._services()[name], name

    def test_production_does_not_bind_mount_the_source_tree(self):
        for name, service in self._services().items():
            for volume in service.get("volumes", []):
                if isinstance(volume, str):
                    assert not volume.startswith(".:/app"), name

    def test_nginx_serves_the_shared_volumes_and_waits_for_a_healthy_web(self):
        nginx = self._services()["nginx"]
        volumes = " ".join(nginx["volumes"])
        assert "staticfiles" in volumes and "media" in volumes
        assert nginx["depends_on"]["web"]["condition"] == "service_healthy"


class TestNginx:
    def test_static_and_media_are_served_from_the_shared_volume(self):
        conf = _read("deploy/nginx.conf")
        assert "location /static/" in conf
        assert "alias /app/staticfiles/;" in conf
        assert "location /media/" in conf
        assert "alias /app/media/;" in conf

    def test_the_proxy_forwards_the_headers_django_relies_on(self):
        conf = _read("deploy/nginx.conf")
        for header in ("X-Forwarded-Proto", "X-Forwarded-For", "X-Forwarded-Host", "Host"):
            assert f"proxy_set_header {header}" in conf
        assert "proxy_pass http://flashwear_app;" in conf

    def test_upload_size_is_bounded_at_the_proxy(self):
        assert "client_max_body_size" in _read("deploy/nginx.conf")


class TestContinuousIntegration:
    """The §60 gates exist as commands someone else can run, not as prose."""

    def test_every_gate_is_a_step_in_the_workflow(self):
        workflow = _read(".github/workflows/ci.yml")
        for command in (
            "ruff check .",
            "ruff format --check .",
            "djlint --lint .",
            "manage.py check",
            "makemigrations --check",
            "check --deploy",
            "collectstatic --noinput",
            "pytest --cov",
            "npm run build:css",
            "docker build",
            "pip_audit",
        ):
            assert command in workflow, command

    def test_the_workflow_parses_and_runs_the_documented_jobs(self):
        workflow = yaml.safe_load(_read(".github/workflows/ci.yml"))
        assert set(workflow["jobs"]) == {"python", "assets", "docker", "audit"}

    def test_settings_level_gates_never_see_a_real_secret(self):
        # The production guards must be satisfied by obviously fake values, proving
        # no real credential is (or needs to be) checked in for CI.
        workflow = _read(".github/workflows/ci.yml")
        assert "ci-dummy-secret-key" in workflow
        assert "ci-dummy-webhook-secret" in workflow
