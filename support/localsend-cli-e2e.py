#!/usr/bin/env python3
"""Exercise a real LocalSend v2 file transfer between two installed CLI peers.

Usage: python3 support/localsend-cli-e2e.py [path/to/localsend-cli]
"""

import argparse
import hashlib
import http.client
import json
import os
import pty
import re
import select
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path


TIMEOUT = 35
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class Peer:
    def __init__(self, name, executable, config_dir, destination, port, file=None):
        master, slave = pty.openpty()
        # Ratatui needs a real terminal size to render its device list.
        import fcntl

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        env = os.environ.copy()
        env.update(XDG_CONFIG_HOME=str(config_dir), TERM="xterm")
        args = [str(executable), "--alias", name, "--port", str(port), "--destination", str(destination)]
        if file is not None:
            args.extend(["--file", str(file)])
        try:
            self.process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self.name = name
        self.master = master
        self.output = bytearray()
        self.port = port
        self.config_dir = config_dir / "localsend-cli"

    def drain(self):
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return
        self.output.extend(chunk)
        if len(self.output) > 131072:
            del self.output[:-131072]

    def send(self, keys):
        os.write(self.master, keys)

    def text(self):
        return ANSI.sub(b"", bytes(self.output)).decode("utf-8", "replace")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)


def pump(peers, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out waiting for CLI transfer")
    readable, _, _ = select.select([peer.master for peer in peers], [], [], min(remaining, 0.25))
    for peer in peers:
        if peer.master in readable:
            peer.drain()


def wait_for(peers, condition, label):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if condition():
            return
        for peer in peers:
            if peer.process.poll() is not None:
                raise RuntimeError(f"{peer.name} exited while waiting for {label}: {peer.process.returncode}")
        pump(peers, deadline)
    raise TimeoutError(f"timed out waiting for {label}")


def reserve_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def register_receiver(sender, receiver):
    """Introduce the receiver to the sender over the real v2 register API."""
    identity = receiver.config_dir / "identity.pem"
    pem = identity.read_text()
    certificate = "-----BEGIN CERTIFICATE-----" + pem.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----"
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest().upper()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.load_cert_chain(str(identity))
    body = json.dumps({
        "alias": receiver.name,
        "version": "2.2",
        "fingerprint": fingerprint,
        "port": receiver.port,
        "protocol": "https",
        "download": False,
    }).encode()
    connection = http.client.HTTPSConnection("127.0.0.1", sender.port, context=context, timeout=3)
    try:
        connection.request("POST", "/api/localsend/v2/register", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise RuntimeError(f"receiver registration returned HTTP {response.status}")
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", default=shutil.which("localsend-cli"))
    parser.add_argument("--log-dir", type=Path, help="save sender and receiver terminal logs for CI diagnostics")
    args = parser.parse_args()
    executable = Path(args.executable or "")
    if not executable.is_file():
        raise RuntimeError("localsend-cli executable not found")
    version = subprocess.check_output([str(executable), "--version"], text=True, timeout=5).strip()
    if version != "localsend-cli 1.18.2":
        raise RuntimeError(f"expected original localsend-cli 1.18.2, got {version!r}")

    peers = []
    try:
        with tempfile.TemporaryDirectory(prefix="localsend-cli-e2e-") as temporary:
            root = Path(temporary)
            source = root / "transfer-proof.bin"
            source.write_bytes(os.urandom(131072) + b"\nLocalSend Homebrew end-to-end transfer\n")
            destination = root / "received"
            destination.mkdir()
            sender_port, receiver_port = reserve_port(), reserve_port()
            while receiver_port == sender_port:
                receiver_port = reserve_port()
            receiver = Peer("E2E Receiver", executable, root / "receiver-config", destination, receiver_port)
            peers.append(receiver)
            wait_for(peers, lambda: (receiver.config_dir / "identity.pem").is_file(), "receiver identity")
            sender = Peer("E2E Sender", executable, root / "sender-config", root, sender_port, source)
            peers.append(sender)
            wait_for(peers, lambda: (sender.config_dir / "identity.pem").is_file(), "sender identity")

            # The HTTP listener can lag identity creation slightly. Registration
            # also proves that the sender server is accepting authenticated TLS.
            deadline = time.monotonic() + TIMEOUT
            while True:
                try:
                    register_receiver(sender, receiver)
                    break
                except (ConnectionError, OSError):
                    if time.monotonic() >= deadline:
                        raise
                    pump(peers, deadline)

            wait_for(peers, lambda: "E2E Receiver" in sender.text(), "receiver in sender device list")
            sender.send(b"\r")
            wait_for(peers, lambda: "Accept? Y/N/P" in receiver.text(), "receiver transfer prompt")
            receiver.send(b"y")
            received = destination / source.name
            wait_for(peers, lambda: received.is_file() and sender.process.poll() is not None, "completed transfer")
            if sender.process.returncode != 0:
                raise RuntimeError(f"sender exited with {sender.process.returncode}")
            actual = hashlib.sha256(received.read_bytes()).hexdigest()
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != expected:
                raise AssertionError(f"received SHA256 {actual} != sent SHA256 {expected}")
            print(f"PASS: two localsend-cli {version.split()[-1]} peers transferred {source.stat().st_size} bytes; SHA256 {actual}")
    except Exception:
        for peer in peers:
            print(f"--- {peer.name} (exit {peer.process.poll()}) ---\n{peer.text()[-8000:]}", file=sys.stderr)
        raise
    finally:
        for peer in reversed(peers):
            peer.close()
        if args.log_dir:
            args.log_dir.mkdir(parents=True, exist_ok=True)
            for peer in peers:
                (args.log_dir / f"{peer.name.lower().replace(' ', '-')}.log").write_text(peer.text())


if __name__ == "__main__":
    main()
