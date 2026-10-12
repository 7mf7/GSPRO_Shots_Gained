#!/usr/bin/env python3
"""
gspro_api.py — Interface to the GSPro API v1 Connect.

GSPro listens on a TCP socket (default 127.0.0.1:921).
This module manages the connection, sends shot data as newline-delimited
JSON, reads game-state responses, and keeps the link alive with heartbeats.

Public API
----------
GSProConnection(host, port)   — create a connection manager
  .connect()                  — open the TCP link to GSPro
  .disconnect()               — close the link
  .send_debug_shot()          — fire the predefined debug shot
  .send_shot(ball, club)      — fire a fully custom shot
  .send_heartbeat()           — send a ready / keep-alive ping
  .is_connected()             — True if the socket is open
  .last_response             — most recent parsed GSPro response dict

DEBUG_SHOT                    — dict of fixed debug parameters
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# ── Protocol constants ─────────────────────────────────────────────────────────

DEFAULT_HOST       = "127.0.0.1"
DEFAULT_PORT       = 921
DEVICE_ID          = "GSpro-OCR-Daemon"
API_VERSION        = "1"
HEARTBEAT_INTERVAL = 10.0   # seconds between keep-alive pings

# ── Debug shot — parameters specified by user ──────────────────────────────────

DEBUG_BALL: dict[str, float] = {
    "Speed":     100.0,   # mph
    "SpinAxis":    0.0,   # degrees  (0 = straight; −ve = left draw; +ve = right fade)
    "TotalSpin": 6500.0,  # RPM
    "HLA":         0.0,   # Horizontal Launch Angle (degrees)
    "VLA":        25.0,   # Vertical Launch Angle (degrees)
}

DEBUG_CLUB: dict[str, float] = {
    "Speed":                0.0,
    "AngleOfAttack":        0.0,
    "FaceToTarget":         0.0,
    "Lie":                  0.0,
    "Loft":                 0.0,
    "Path":                 0.0,
    "SpeedAtImpact":        0.0,
    "VerticalFaceImpact":   0.0,
    "HorizontalFaceImpact": 0.0,
    "ClosureRate":          0.0,
}

DEBUG_SHOT: dict[str, dict] = {
    "BallData": DEBUG_BALL,
    "ClubData": DEBUG_CLUB,
}

# ── GSPro response codes ───────────────────────────────────────────────────────

GSPRO_OK            = 200
GSPRO_PLAYER_INFO   = 201   # player / club information update
GSPRO_ERR_SHOT      = 501   # invalid shot data

_CODE_LABELS = {
    GSPRO_OK:          "Shot accepted",
    GSPRO_PLAYER_INFO: "Player/club update",
    GSPRO_ERR_SHOT:    "Invalid shot data",
}


# ── Connection manager ─────────────────────────────────────────────────────────

class GSProConnection:
    """
    Manages a persistent TCP connection to the GSPro API v1 Connect endpoint.

    Thread-safe: all public methods can be called from any thread.
    Heartbeats and response reads run on background daemon threads.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        on_response: Callable[[dict], None] | None = None,
    ) -> None:
        self.host          = host
        self.port          = port
        self._on_response  = on_response     # called with every parsed response

        self._sock: socket.socket | None = None
        self._lock         = threading.Lock()
        self._running      = False
        self._shot_number  = 0
        self.last_response: dict[str, Any] = {}

    # ── public ────────────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        """
        Open a TCP connection to GSPro.
        Starts background threads for heartbeats and response reading.
        Returns True on success.
        """
        with self._lock:
            if self._sock is not None:
                log.info("GSPro: already connected to %s:%d", self.host, self.port)
                return True

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((self.host, self.port))
                sock.settimeout(None)   # blocking mode for the recv thread
                self._sock    = sock
                self._running = True
                log.info("GSPro: ✔  connected to %s:%d", self.host, self.port)
            except (OSError, ConnectionRefusedError) as exc:
                log.error("GSPro: ✘  connection failed — %s", exc)
                log.error("GSPro:    Is GSPro running with API Connect enabled on port %d?", self.port)
                self._sock = None
                return False

        # Threads started outside the lock so _lock is free during I/O
        threading.Thread(target=self._recv_loop,      daemon=True, name="gspro-recv").start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="gspro-hb").start()

        # Send an initial heartbeat so GSPro knows we're ready
        time.sleep(0.1)
        self.send_heartbeat()
        return True

    def disconnect(self) -> None:
        """Close the connection and stop background threads."""
        with self._lock:
            self._running = False
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                log.info("GSPro: disconnected")

    def send_debug_shot(self) -> bool:
        """
        Send the predefined debug shot:
          Ball speed 100 mph  |  VLA 25°  |  HLA 0°
          Total spin 6500 RPM |  Spin axis 0°  |  All club data 0
        """
        log.info(
            "GSPro: → debug shot  speed=%.0f mph  VLA=%.1f°  HLA=%.1f°  "
            "spin=%.0f RPM  axis=%.1f°",
            DEBUG_BALL["Speed"], DEBUG_BALL["VLA"], DEBUG_BALL["HLA"],
            DEBUG_BALL["TotalSpin"], DEBUG_BALL["SpinAxis"],
        )
        return self.send_shot(DEBUG_BALL, DEBUG_CLUB)

    def send_shot(
        self,
        ball_data: dict[str, float],
        club_data: dict[str, float],
    ) -> bool:
        """
        Send a full shot to GSPro.

        ball_data keys: Speed, SpinAxis, TotalSpin, HLA, VLA
        club_data keys: Speed, AngleOfAttack, FaceToTarget, Lie, Loft,
                        Path, SpeedAtImpact, VerticalFaceImpact,
                        HorizontalFaceImpact, ClosureRate
        """
        self._shot_number += 1
        payload = self._build_payload(ball_data, club_data, heartbeat=False)
        return self._send(payload)

    def send_heartbeat(self) -> bool:
        """Send a ready / keep-alive ping (IsHeartBeat = true)."""
        zero_ball = {k: 0.0 for k in DEBUG_BALL}
        zero_club = {k: 0.0 for k in DEBUG_CLUB}
        payload   = self._build_payload(zero_ball, zero_club, heartbeat=True)
        ok        = self._send(payload)
        if ok:
            log.debug("GSPro: → heartbeat")
        return ok

    # ── internal ──────────────────────────────────────────────────────────────

    def _build_payload(
        self,
        ball: dict[str, float],
        club: dict[str, float],
        heartbeat: bool,
    ) -> dict:
        return {
            "DeviceID":   DEVICE_ID,
            "Units":      "Yards",
            "ShotNumber": 0 if heartbeat else self._shot_number,
            "APIversion": API_VERSION,
            "BallData":   ball,
            "ClubData":   club,
            "ShotDataOptions": {
                "ContainsBallData":          not heartbeat,
                "ContainsClubData":          not heartbeat,
                "LaunchMonitorIsReady":      True,
                "LaunchMonitorBallDetected": not heartbeat,
                "IsHeartBeat":               heartbeat,
            },
        }

    def _send(self, payload: dict) -> bool:
        with self._lock:
            if self._sock is None:
                log.error("GSPro: not connected — press Ctrl+Shift+F7 to connect first")
                return False
            try:
                data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
                self._sock.sendall(data)
                return True
            except OSError as exc:
                log.error("GSPro: send failed — %s — connection lost", exc)
                self._sock = None
                return False

    def _recv_loop(self) -> None:
        """Background thread: read and parse newline-delimited JSON from GSPro."""
        buf = ""
        while self._running:
            try:
                sock = self._sock
                if sock is None:
                    break
                chunk = sock.recv(4096)
                if not chunk:
                    log.info("GSPro: server closed the connection")
                    self._sock = None
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            self._handle_response(json.loads(line))
                        except json.JSONDecodeError:
                            log.warning("GSPro: non-JSON response: %r", line[:120])
            except OSError:
                break
        log.debug("GSPro: recv thread exited")

    def _handle_response(self, msg: dict) -> None:
        """Parse and log a GSPro response message."""
        self.last_response = msg
        code   = msg.get("Code", "?")
        text   = msg.get("Message", "")
        player = msg.get("Player", {})
        club   = player.get("Club", "")
        handed = player.get("Handed", "")
        idx    = player.get("Index", "")

        label = _CODE_LABELS.get(code, f"code {code}")
        if club:
            log.info("GSPro: ← %s (%s)  club=%s  handed=%s  index=%s",
                     label, text, club, handed, idx)
        else:
            log.info("GSPro: ← %s (%s)", label, text)

        if self._on_response:
            try:
                self._on_response(msg)
            except Exception as exc:
                log.error("GSPro: on_response callback raised: %s", exc)

    def _heartbeat_loop(self) -> None:
        """Background thread: send a heartbeat every HEARTBEAT_INTERVAL seconds."""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            if self._running and self._sock is not None:
                self.send_heartbeat()
        log.debug("GSPro: heartbeat thread exited")
