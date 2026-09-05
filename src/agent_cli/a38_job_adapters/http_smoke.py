"""HTTP smoke adapter: build an image, refuse no-auth start, then health/auth checks."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urljoin, urlsplit

from .common import (
    COMMON_KEYS,
    DOCKER_HEAVY_LOCK,
    JobError,
    JobRuntime,
    CommonConfig,
    expand_placeholders,
    loads_strict_json,
    parse_common_config,
    parse_docker_host_port,
    reject_unknown_keys,
    require_env_name,
    require_docker_id,
    require_finite_number,
    require_mapping,
    require_rel_path,
    require_str,
    run_lifecycle,
    safe_join,
    validate_placeholders,
)

MAX_HTTP_BODY_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_URL_PATH_CHARS = 4096
MAX_REDIRECTS = 5

HTTP_SMOKE_KEYS = COMMON_KEYS | frozenset(
    {
        "dockerfile",
        "platform",
        "build_args",
        "container_port",
        "credentials",
        "health",
        "root_path",
        "manifest",
    }
)
CREDENTIAL_KEYS = frozenset({"user_env", "password_env", "user", "password"})
HEALTH_KEYS = frozenset({"path", "contains"})
MANIFEST_KEYS = frozenset(
    {
        "path",
        "artifacts_key",
        "category_key",
        "path_key",
        "index",
        "pdf_category",
    }
)


def parse_http_smoke_config(text: str) -> tuple[CommonConfig, dict[str, Any]]:
    raw = loads_strict_json(text)
    obj = require_mapping(raw, "config")
    reject_unknown_keys(obj, HTTP_SMOKE_KEYS, "http-smoke config")
    required = frozenset(
        {
            "dockerfile",
            "platform",
            "container_port",
            "credentials",
            "health",
            "root_path",
            "manifest",
        }
    )
    missing = required - set(obj)
    if missing:
        raise JobError(f"http-smoke config missing keys: {', '.join(sorted(missing))}")

    dockerfile = require_rel_path(obj["dockerfile"], "dockerfile")
    platform = require_str(obj["platform"], "platform")
    build_args: dict[str, str] = {}
    if "build_args" in obj:
        args_obj = require_mapping(obj["build_args"], "build_args")
        for key, value in args_obj.items():
            if not isinstance(key, str) or not key or "\x00" in key:
                raise JobError("build_args keys must be non-empty strings")
            if not isinstance(value, str) or "\x00" in value:
                raise JobError(f"build_args.{key} must be a string without NUL")
            validate_placeholders(value, label=f"build_args.{key}")
            build_args[key] = value
    port = require_finite_number(obj["container_port"], "container_port")
    if port != int(port) or int(port) <= 0 or int(port) > 65535:
        raise JobError("container_port must be an integer 1..65535")

    credentials = require_mapping(obj["credentials"], "credentials")
    reject_unknown_keys(credentials, CREDENTIAL_KEYS, "credentials")
    if set(credentials) != CREDENTIAL_KEYS:
        raise JobError("credentials requires user_env, password_env, user, and password")
    cred_cfg = {
        "user_env": require_env_name(credentials["user_env"], "credentials.user_env"),
        "password_env": require_env_name(credentials["password_env"], "credentials.password_env"),
        "user": require_str(credentials["user"], "credentials.user"),
        "password": require_str(credentials["password"], "credentials.password"),
    }

    health = require_mapping(obj["health"], "health")
    reject_unknown_keys(health, HEALTH_KEYS, "health")
    if set(health) != HEALTH_KEYS:
        raise JobError("health requires path and contains")
    health_cfg = {
        "path": _safe_url_path(require_str(health["path"], "health.path"), "health.path"),
        "contains": require_str(health["contains"], "health.contains"),
    }

    root_path = _safe_url_path(require_str(obj["root_path"], "root_path"), "root_path")

    manifest = require_mapping(obj["manifest"], "manifest")
    reject_unknown_keys(manifest, MANIFEST_KEYS, "manifest")
    if set(manifest) != MANIFEST_KEYS:
        raise JobError("manifest is missing required keys")
    manifest_cfg = {
        "path": require_str(manifest["path"], "manifest.path"),
        "artifacts_key": require_str(manifest["artifacts_key"], "manifest.artifacts_key"),
        "category_key": require_str(manifest["category_key"], "manifest.category_key"),
        "path_key": require_str(manifest["path_key"], "manifest.path_key"),
        "index": require_str(manifest["index"], "manifest.index"),
        "pdf_category": require_str(manifest["pdf_category"], "manifest.pdf_category"),
    }
    if (
        not manifest_cfg["path"].startswith("/")
        or ".." in Path(manifest_cfg["path"]).parts
        or "\\" in manifest_cfg["path"]
    ):
        raise JobError("manifest.path must be an absolute container path without ..")
    index_path = manifest_cfg["index"]
    if (
        index_path.startswith("/")
        or ".." in Path(index_path).parts
        or "\\" in index_path
        or len(index_path) > MAX_URL_PATH_CHARS
    ):
        raise JobError("manifest.index must be a safe relative URL path")

    common = parse_common_config(obj)
    return common, {
        "dockerfile": dockerfile,
        "platform": platform,
        "build_args": build_args,
        "container_port": int(port),
        "credentials": cred_cfg,
        "health": health_cfg,
        "root_path": root_path,
        "manifest": manifest_cfg,
    }


def _safe_url_path(path: str, label: str) -> str:
    if (
        "\x00" in path
        or "://" in path
        or "\\" in path
        or "?" in path
        or "#" in path
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
    ):
        raise JobError(f"{label} is not a safe URL path")
    if not path.startswith("/"):
        raise JobError(f"{label} must start with /")
    if len(path) > MAX_URL_PATH_CHARS:
        raise JobError(f"{label} is too long")
    if any(part == ".." for part in path.split("/")):
        raise JobError(f"{label} must not contain ..")
    return path


def _encode_url_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise JobError("HTTP smoke requests must target a loopback HTTP origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise JobError("HTTP smoke URL has an invalid port") from exc
    if port is None or parsed.username is not None or parsed.password is not None:
        raise JobError("HTTP smoke URL must contain an explicit port and no userinfo")
    return parsed.scheme, parsed.hostname, port


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, original_url: str) -> None:
        super().__init__()
        self._origin = _origin(original_url)
        self._remaining = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urljoin(req.full_url, newurl)
        if _origin(target) != self._origin:
            raise JobError("HTTP redirect left the original loopback origin")
        if self._remaining <= 0:
            raise JobError("HTTP redirect limit exceeded")
        self._remaining -= 1
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None:
            authorization = req.get_header("Authorization")
            if authorization:
                redirected.add_unredirected_header("Authorization", authorization)
        return redirected


def _read_bounded(response: Any, limit: int | None = None) -> bytes:
    if limit is None:
        limit = MAX_HTTP_BODY_BYTES
    body = response.read(limit + 1)
    if len(body) > limit:
        raise JobError(f"HTTP response exceeds {limit} bytes")
    return body


def _http_request(
    url: str,
    *,
    method: str = "GET",
    auth: tuple[str, str] | None = None,
    timeout_s: float = 5.0,
    follow_redirects: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    headers: dict[str, str] = {}
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, method=method, headers=headers)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            return None

    _origin(url)
    # Never consult ambient proxy variables for loopback smoke traffic. Besides
    # making local checks deterministic, this prevents Basic Auth from reaching
    # a configured external proxy.
    handlers: list[Any] = [urllib.request.ProxyHandler({})]
    handlers.append(_SameOriginRedirect(url) if follow_redirects else _NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout_s) as response:
            body = _read_bounded(response)
            headers_out = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), body, headers_out
    except urllib.error.HTTPError as exc:
        body = _read_bounded(exc) if exc.fp is not None else b""
        headers_out = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return int(exc.code), body, headers_out
    except urllib.error.URLError as exc:
        raise JobError(f"HTTP request failed: {exc.reason}") from exc


def _redact(text: str, secrets: Sequence[str]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out

def _body(runtime: JobRuntime, cfg: Mapping[str, Any]) -> int:
    docker = shutil.which("docker", path=runtime.env.get("PATH"))
    if not docker:
        raise JobError("required command not found: docker")

    texts = list(runtime.common.env.values()) + list(cfg["build_args"].values())
    runtime.ensure_image_placeholders(texts)
    runtime.refresh_configured_env()

    image = runtime.image_tag("http-smoke")
    smoke_name = f"a38-http-smoke-{runtime.run_id}"
    noauth_name = f"a38-http-noauth-{runtime.run_id}"
    work = runtime.artifacts / "http-smoke"
    work.mkdir(parents=True, exist_ok=True)

    state: dict[str, str | None] = {"smoke_id": None, "noauth_id": None, "image": None}
    secrets = [cfg["credentials"]["user"], cfg["credentials"]["password"]]

    def cleanup(original: int) -> int:
        del original
        for key in ("smoke_id", "noauth_id"):
            cid = state[key]
            if not cid:
                continue
            if not runtime.interrupted:
                try:
                    raw_dir = runtime.work / "http-smoke-logs"
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    raw_path = raw_dir / f"{key}.log"
                    with raw_path.open("wb") as handle:
                        runtime.bounded(
                            15,
                            [docker, "logs", cid],
                            stdout=handle,
                            stderr=subprocess.STDOUT,
                        )
                    with raw_path.open("rb") as handle:
                        raw = handle.read(MAX_HTTP_BODY_BYTES + 1)
                    if len(raw) > MAX_HTTP_BODY_BYTES:
                        raw = raw[:MAX_HTTP_BODY_BYTES] + b"\n[a38: log truncated]\n"
                    text = raw.decode("utf-8", "replace")
                    (work / f"{key}.log").write_text(
                        _redact(text, secrets), encoding="utf-8"
                    )
                except OSError as exc:
                    print(f"a38: warning: could not capture container logs: {exc}", file=sys.stderr)
            try:
                rm = runtime.bounded(20, [docker, "rm", "-f", cid])
            except OSError as exc:
                print(f"a38: warning: could not remove owned container: {exc}", file=sys.stderr)
            else:
                if rm.returncode == 0:
                    continue
                print(
                    f"a38: warning: could not remove owned container {cid}; inspect it on this host",
                    file=sys.stderr,
                )
        if state["image"]:
            try:
                rm_img = runtime.bounded(20, [docker, "image", "rm", image])
            except OSError as exc:
                print(f"a38: warning: could not remove owned image: {exc}", file=sys.stderr)
            else:
                if rm_img.returncode == 0:
                    return 0
                print(
                    f"a38: warning: could not remove owned image {image}; inspect it on this host",
                    file=sys.stderr,
                )
        return 0

    runtime.set_job_cleanup(cleanup)

    dockerfile = safe_join(runtime.root, cfg["dockerfile"])
    if not dockerfile.is_file():
        raise JobError(f"Dockerfile does not exist: {cfg['dockerfile']}")

    build_argv = [
        docker,
        "build",
        "--platform",
        cfg["platform"],
        "-f",
        str(dockerfile),
        "-t",
        image,
    ]
    for key, value in cfg["build_args"].items():
        expanded = expand_placeholders(value, mapping=runtime.mapping, images=runtime.images)
        build_argv.extend(["--build-arg", f"{key}={expanded}"])
    build_argv.append(str(runtime.root))
    print(f"a38: building http-smoke image {image} (platform={cfg['platform']})", flush=True)
    built = runtime.run_argv(build_argv, stdout=sys.stdout, stderr=sys.stderr, check=False)
    if built.returncode != 0:
        raise JobError("http-smoke image build failed")
    # A successful build owns the unique tag even if the following inspect fails.
    state["image"] = image
    runtime.track_image(image)
    inspect = runtime.run_argv(
        [docker, "image", "inspect", "--format", "{{.Id}}", image],
        check=False,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        raise JobError("failed to inspect built http-smoke image")
    create = runtime.run_argv([docker, "create", "--name", noauth_name, image], check=False)
    if create.returncode != 0:
        raise JobError("failed to create no-auth container")
    noauth_id = require_docker_id(create.stdout.strip(), "docker create")
    state["noauth_id"] = noauth_id
    start = runtime.run_argv([docker, "start", "-a", state["noauth_id"]], check=False)
    if start.returncode == 0:
        raise JobError("expected container to exit non-zero without credentials")
    print("a38: entrypoint correctly refused start without credentials", flush=True)

    user_env = cfg["credentials"]["user_env"]
    password_env = cfg["credentials"]["password_env"]
    create_smoke = runtime.run_argv(
        [
            docker,
            "create",
            "--name",
            smoke_name,
            "-p",
            f"127.0.0.1::{cfg['container_port']}",
            "-e",
            f"{user_env}={cfg['credentials']['user']}",
            "-e",
            f"{password_env}={cfg['credentials']['password']}",
            image,
        ],
        check=False,
    )
    if create_smoke.returncode != 0:
        raise JobError("failed to create http-smoke container")
    smoke_id = require_docker_id(create_smoke.stdout.strip(), "docker create")
    state["smoke_id"] = smoke_id
    started = runtime.run_argv([docker, "start", smoke_id], check=False)
    if started.returncode != 0:
        raise JobError("failed to start http-smoke container")

    port_out = runtime.run_argv(
        [docker, "port", smoke_id, f"{cfg['container_port']}/tcp"],
        check=False,
    )
    if port_out.returncode != 0:
        raise JobError("failed to resolve http-smoke port")
    host_port = parse_docker_host_port(port_out.stdout, "http-smoke")
    base = f"http://127.0.0.1:{host_port}"
    health_url = f"{base}{cfg['health']['path']}"

    healthy = False
    for _ in range(20):
        try:
            code, body, _headers = _http_request(health_url, timeout_s=3.0)
            if code == 200 and cfg["health"]["contains"].encode("utf-8") in body:
                healthy = True
                break
        except JobError:
            pass
        time.sleep(1)
    if not healthy:
        raise JobError("health check did not become ready")

    code, body, _headers = _http_request(health_url, timeout_s=5.0)
    if code != 200 or cfg["health"]["contains"].encode("utf-8") not in body:
        raise JobError("health check body/status mismatch")

    root_url = f"{base}{cfg['root_path']}"
    code, _body, _headers = _http_request(root_url, timeout_s=5.0)
    if code != 401:
        raise JobError(f"expected 401 from root without auth, got {code}")

    auth = (cfg["credentials"]["user"], cfg["credentials"]["password"])
    code, _body, _headers = _http_request(
        root_url, auth=auth, timeout_s=5.0, follow_redirects=True
    )
    if code != 200:
        raise JobError(f"expected 200 from root with auth, got {code}")

    manifest_host = work / "manifest.json"
    copy = runtime.run_argv(
        [docker, "cp", f"{state['smoke_id']}:{cfg['manifest']['path']}", str(manifest_host)],
        check=False,
    )
    if copy.returncode != 0 or not manifest_host.is_file():
        raise JobError("failed to copy manifest from container")

    try:
        if manifest_host.stat().st_size > MAX_MANIFEST_BYTES:
            raise JobError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        manifest_data = loads_strict_json(manifest_host.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise JobError("manifest is not UTF-8") from exc
    if not isinstance(manifest_data, dict):
        raise JobError("manifest root must be an object")
    artifacts = manifest_data.get(cfg["manifest"]["artifacts_key"])
    if not isinstance(artifacts, list) or not artifacts:
        raise JobError("manifest has no artifacts")

    by_category: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            raise JobError(f"manifest artifacts[{index}] must be an object")
        category = item.get(cfg["manifest"]["category_key"])
        path_value = item.get(cfg["manifest"]["path_key"])
        if not isinstance(category, str) or not category:
            raise JobError(f"manifest artifacts[{index}] category must be a non-empty string")
        if not isinstance(path_value, str) or not path_value or "\x00" in path_value:
            raise JobError(f"manifest artifacts[{index}] path must be a non-empty string")
        if path_value.startswith("/") or ".." in Path(path_value).parts or "\\" in path_value:
            raise JobError(f"manifest artifacts[{index}] path is not a safe relative URL path")
        if len(path_value) > MAX_URL_PATH_CHARS:
            raise JobError(f"manifest artifacts[{index}] path is too long")
        if category not in by_category:
            by_category[category] = item

    sample_paths = [cfg["manifest"]["index"]]
    for item in by_category.values():
        path_value = str(item[cfg["manifest"]["path_key"]])
        if path_value != cfg["manifest"]["index"]:
            sample_paths.append(path_value)

    for path_value in sample_paths:
        encoded = _encode_url_path(path_value)
        code, _body, _headers = _http_request(
            f"{base}/{encoded}", auth=auth, timeout_s=10.0
        )
        if code < 200 or code >= 300:
            print(f"artifact sample {code}: {path_value}", file=sys.stderr)
            raise JobError("artifact sample failed")
        print(f"ok {code} {path_value}", flush=True)

    pdf_item = by_category.get(cfg["manifest"]["pdf_category"])
    if pdf_item is not None:
        pdf_path = str(pdf_item[cfg["manifest"]["path_key"]])
        encoded = _encode_url_path(pdf_path)
        code, _body, headers = _http_request(
            f"{base}/{encoded}", auth=auth, timeout_s=10.0
        )
        if code < 200 or code >= 300:
            raise JobError(f"pdf sample {code}: {pdf_path}")
        disposition = headers.get("content-disposition", "")
        # Never print configured credentials.
        safe_disp = _redact(disposition, secrets)
        print(f"Content-Disposition for {pdf_path}: {safe_disp}", flush=True)
        if "inline" not in disposition.lower():
            raise JobError("expected Content-Disposition: inline for PDF")

    print("a38: http-smoke passed", flush=True)
    return 0


def run_http_smoke(
    config_text: str,
    *,
    cwd: Path | None = None,
    lock_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    common, parsed = parse_http_smoke_config(config_text)

    def body(runtime: JobRuntime) -> int:
        return _body(runtime, parsed)

    return run_lifecycle(
        adapter="http-smoke",
        common=common,
        body=body,
        cwd=cwd,
        lock_root=lock_root,
        environ=environ,
        default_lock=DOCKER_HEAVY_LOCK,
    )
