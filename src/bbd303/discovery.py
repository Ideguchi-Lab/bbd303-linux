"""Bounded Ethernet discovery for DHCP-connected BBD303 controllers."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct

from .config import Config
from .errors import DiscoveryError
from .models import DiscoveryResult

_HW_REQ_INFO = bytes.fromhex("050000001101")  # destination 0x11: BBD30x3 motherboard
_MAX_SCAN_HOSTS = 4096


def split_connection(connection: str, default_port: int = 40303) -> tuple[str, int]:
    host, separator, port_text = connection.rpartition(":")
    if not separator:
        host, port_text = connection, str(default_port)
    try:
        ipaddress.IPv4Address(host)
        port = int(port_text)
    except ValueError as exc:
        raise DiscoveryError(f"Invalid IPv4 connection: {connection}") from exc
    if not 1 <= port <= 65535:
        raise DiscoveryError(f"Invalid TCP port: {port}")
    return host, port


def _read_exact(stream: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = stream.recv(length - len(result))
        if not chunk:
            raise ConnectionError("Controller closed the connection")
        result.extend(chunk)
    return bytes(result)


def probe(connection: str, expected_serial: str | None = None, timeout: float = 0.3) -> DiscoveryResult | None:
    """Read hardware identity without opening a motion-control channel."""
    host, port = split_connection(connection)
    try:
        with socket.create_connection((host, port), timeout=timeout) as stream:
            stream.settimeout(timeout)
            stream.sendall(_HW_REQ_INFO)
            header = _read_exact(stream, 6)
            if struct.unpack_from("<H", header)[0] != 0x0006 or not header[4] & 0x80:
                return None
            payload_length = struct.unpack_from("<H", header, 2)[0]
            payload = _read_exact(stream, payload_length)
    except (OSError, ConnectionError):
        return None
    if len(payload) < 12:
        return None
    serial = str(struct.unpack_from("<I", payload)[0])
    model = payload[4:12].decode("ascii", errors="replace").rstrip("\0 ")
    if expected_serial is not None and serial != expected_serial:
        return None
    return DiscoveryResult(f"{host}:{port}", serial, model, "probe")


def _load_cache(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return str(value["connection"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _save_cache(path: Path, result: DiscoveryResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "connection": result.connection,
        "serial": result.serial,
        "model": result.model,
    }, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _scan_candidates(config: Config) -> list[str]:
    candidates: list[str] = []
    count = 0
    for text in config.scan_subnets:
        try:
            network = ipaddress.IPv4Network(text, strict=False)
        except ValueError as exc:
            raise DiscoveryError(f"Invalid discovery subnet: {text}") from exc
        hosts = list(network.hosts())
        count += len(hosts)
        if count > _MAX_SCAN_HOSTS:
            raise DiscoveryError(f"Configured subnets exceed {_MAX_SCAN_HOSTS} scan hosts")
        candidates.extend(f"{address}:{config.port}" for address in hosts)
    return candidates


def discover(config: Config, connection: str | None = None, refresh: bool = False) -> DiscoveryResult:
    attempts: list[tuple[str, str]] = []
    if connection:
        host, port = split_connection(connection, config.port)
        attempts.append((f"{host}:{port}", "explicit"))
    if not refresh:
        cached = _load_cache(config.cache_path)
        if cached and all(cached != item[0] for item in attempts):
            attempts.append((cached, "cache"))

    for candidate, source in attempts:
        result = probe(candidate, config.serial)
        if result and result.model == config.model:
            result = DiscoveryResult(result.connection, result.serial, result.model, source)
            _save_cache(config.cache_path, result)
            return result
    if connection:
        raise DiscoveryError(f"Configured controller was not found at {connection}")

    candidates = _scan_candidates(config)
    with ThreadPoolExecutor(max_workers=min(32, len(candidates) or 1)) as pool:
        futures = {pool.submit(probe, item, config.serial, 0.3): item for item in candidates}
        for future in as_completed(futures):
            result = future.result()
            if result and result.model == config.model:
                result = DiscoveryResult(result.connection, result.serial, result.model, "scan")
                for pending in futures:
                    pending.cancel()
                _save_cache(config.cache_path, result)
                return result
    raise DiscoveryError(
        f"BBD303 serial {config.serial} was not found in: {', '.join(config.scan_subnets)}"
    )

