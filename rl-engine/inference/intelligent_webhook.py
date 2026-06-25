#!/usr/bin/env python3
"""
intelligent_webhook.py

Production inference webhook for the Autonomous Incident Remediation system.

Pipeline:
    Prometheus -> Alertmanager -> intelligent_webhook.py -> DQN -> Ansible -> Recovery

Receives Alertmanager webhook callbacks, builds a 6-element state vector from
live Prometheus metrics, runs DQN inference to select a remediation action,
and dispatches the corresponding Ansible playbook.

Action space (must match aiops_env.py / train_dqn.py exactly):
    0 -> restart_nginx.yml
    1 -> restart_node_exporter.yml
    2 -> restart_all_services.yml
    3 -> no action

Python 3.10+
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from logging import Logger
from typing import Optional

import requests
import torch
import torch.nn as nn
from flask import Flask, jsonify, request

# --------------------------------------------------------------------------- #
# Configuration (override via environment variables)
# --------------------------------------------------------------------------- #

MODEL_PATH = os.environ.get("AIOPS_MODEL_PATH", "../models/aiops_dqn.pth")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
PROMETHEUS_TIMEOUT_SECONDS = float(os.environ.get("PROMETHEUS_TIMEOUT_SECONDS", "5"))
PLAYBOOK_DIR = os.environ.get("ANSIBLE_PLAYBOOK_DIR", "playbooks")
ANSIBLE_INVENTORY = os.environ.get("ANSIBLE_INVENTORY", "inventory.ini")
ANSIBLE_TIMEOUT_SECONDS = int(os.environ.get("ANSIBLE_TIMEOUT_SECONDS", "120"))
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "5001"))

# State vector ordering -- MUST match aiops_env.py observation_space exactly.
STATE_KEYS = (
    "web_target_up",
    "data_target_up",
    "cpu_utilization",
    "memory_utilization",
    "spot_exporter_up",
    "system_load",
)

# DQN dimensions -- MUST mirror the QNetwork defined in train_dqn.py.
# If hidden layer sizes differ from train_dqn.py, torch.load_state_dict()
# will raise a shape-mismatch error at load time.
STATE_DIM = 6
ACTION_DIM = 4
HIDDEN_DIM = int(os.environ.get("AIOPS_HIDDEN_DIM", "64"))

# Action ID -> playbook filename. 3 is a deliberate no-op.
ACTION_PLAYBOOK_MAP: dict[int, Optional[str]] = {
    0: "restart_nginx.yml",
    1: "restart_node_exporter.yml",
    2: "restart_all_services.yml",
    3: None,
}

ACTION_LABELS: dict[int, str] = {
    0: "RESTART_NGINX",
    1: "RESTART_NODE_EXPORTER",
    2: "RESTART_ALL_SERVICES",
    3: "NO_ACTION",
}

# --------------------------------------------------------------------------- #
# Structured JSON logging
# --------------------------------------------------------------------------- #


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON for downstream log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra_fields = getattr(record, "extra_fields", None)
        if extra_fields:
            payload.update(extra_fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _build_logger() -> Logger:
    logger = logging.getLogger("intelligent_webhook")
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


log = _build_logger()


def log_event(level: str, message: str, **fields) -> None:
    getattr(log, level)(message, extra={"extra_fields": fields})


# --------------------------------------------------------------------------- #
# DQN model definition (must match train_dqn.py exactly)
# --------------------------------------------------------------------------- #


class QNetwork(nn.Module):
    """
    Feed-forward Q-network mapping the 6D state vector to Q-values for the
    4 discrete remediation actions.

    WARNING: This architecture must be identical to the QNetwork class in
    train_dqn.py (same layer count / widths). If you changed hidden layer
    sizes there, update HIDDEN_DIM (or this class) to match, or
    load_state_dict() will fail with a shape mismatch.
    """

    def __init__(self, state_dim: int = STATE_DIM, action_dim: int = ACTION_DIM, hidden_dim: int = HIDDEN_DIM):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, action_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


# --------------------------------------------------------------------------- #
# Model loading -- fails safe, never crashes the webhook
# --------------------------------------------------------------------------- #


class ModelHandle:
    """
    Thread-safe holder for the loaded DQN. If the model file is missing,
    unreadable, or shape-mismatched, `self.model` stays None and `predict()`
    falls back to the safe default action (3 / NO_ACTION) instead of
    crashing the webhook or restarting services blindly.
    """

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.path = path
        self.model: Optional[QNetwork] = None
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.load()

    def load(self) -> bool:
        with self._lock:
            if not os.path.isfile(self.path):
                log_event(
                    "error",
                    "DQN model file not found; webhook running in fail-safe (no-action) mode",
                    model_path=self.path,
                )
                self.model = None
                return False
            try:
                net = QNetwork().to(self.device)
                state_dict = torch.load(self.path, map_location=self.device)
                net.load_state_dict(state_dict)
                net.eval()
                self.model = net
                log_event(
                    "info",
                    "DQN model loaded successfully",
                    model_path=self.path,
                    device=str(self.device),
                )
                return True
            except Exception as exc:  # noqa: BLE001 - any load failure must fail safe, not crash
                log_event(
                    "error",
                    "Failed to load DQN model; webhook running in fail-safe (no-action) mode",
                    model_path=self.path,
                    error=str(exc),
                )
                self.model = None
                return False

    def predict(self, state_vector: list[float]) -> int:
        """Returns the selected action ID, defaulting to NO_ACTION (3) on any failure."""
        if self.model is None:
            log_event("warning", "Model unavailable, defaulting to NO_ACTION")
            return 3
        try:
            with torch.no_grad():
                tensor = torch.tensor(state_vector, dtype=torch.float32, device=self.device).unsqueeze(0)
                q_values = self.model(tensor)
                action = int(torch.argmax(q_values, dim=1).item())
                log_event(
                    "info",
                    "DQN inference complete",
                    q_values=q_values.squeeze(0).tolist(),
                    selected_action=action,
                    selected_action_label=ACTION_LABELS.get(action, "UNKNOWN"),
                )
                return action
        except Exception as exc:  # noqa: BLE001
            log_event("error", "DQN inference failed, defaulting to NO_ACTION", error=str(exc))
            return 3


model_handle = ModelHandle(MODEL_PATH)

# --------------------------------------------------------------------------- #
# Prometheus client
# --------------------------------------------------------------------------- #

# PromQL per state component, matching the project's Prometheus scrape jobs
# (job="web", job="data", job="spot-price-exporter"). Keys MUST match STATE_KEYS.
PROM_QUERIES: dict[str, str] = {
    "web_target_up": 'up{job="web"}',
    "data_target_up": 'up{job="data"}',
    "cpu_utilization": '(1 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m])))',
    "memory_utilization": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
    "spot_exporter_up": 'up{job="spot-price-exporter"}',
    "system_load": "clamp_max(node_load1 / 4, 3)",
}

# Conservative fallbacks if a metric can't be retrieved: assume the web and
# data targets are up, zero out utilization/load, and assume the spot-price
# exporter is unavailable. This biases the agent toward NO_ACTION rather
# than triggering restarts on bad/missing data.
METRIC_DEFAULTS: dict[str, float] = {
    "web_target_up": 1.0,
    "data_target_up": 1.0,
    "cpu_utilization": 0.0,
    "memory_utilization": 0.0,
    "spot_exporter_up": 0.0,
    "system_load": 0.0,
}


def query_prometheus_instant(promql: str) -> Optional[float]:
    """
    Executes a PromQL instant query. Returns the (mean, if multi-series)
    scalar result, or None if Prometheus is unreachable, errors, or the
    result set is empty/unparseable.
    """
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL.rstrip('/')}/api/v1/query",
            params={"query": promql},
            timeout=PROMETHEUS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.exceptions.RequestException as exc:
        log_event("error", "Prometheus request failed", query=promql, error=str(exc))
        return None
    except ValueError as exc:
        log_event("error", "Prometheus returned invalid JSON", query=promql, error=str(exc))
        return None

    if payload.get("status") != "success":
        log_event("error", "Prometheus query did not succeed", query=promql, response=payload)
        return None

    result = payload.get("data", {}).get("result", [])
    if not result:
        log_event("warning", "Prometheus query returned no data points", query=promql)
        return None

    try:
        values = [float(series["value"][1]) for series in result]
        return sum(values) / len(values)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        log_event("error", "Failed to parse Prometheus result", query=promql, error=str(exc), raw=result)
        return None


def build_state_vector() -> list[float]:
    """
    Queries Prometheus for each required metric and assembles the 6-element
    state vector in the fixed STATE_KEYS order. Any metric that can't be
    retrieved falls back to METRIC_DEFAULTS and is flagged in the logs.
    """
    state: list[float] = []
    degraded_metrics: list[str] = []

    for key in STATE_KEYS:
        value = query_prometheus_instant(PROM_QUERIES[key])
        if value is None:
            value = METRIC_DEFAULTS[key]
            degraded_metrics.append(key)
        state.append(float(value))

    if degraded_metrics:
        log_event(
            "warning",
            "Built state vector with fallback values for one or more metrics",
            degraded_metrics=degraded_metrics,
            state_vector=dict(zip(STATE_KEYS, state)),
        )
    else:
        log_event(
            "info",
            "Built state vector from live Prometheus metrics",
            state_vector=dict(zip(STATE_KEYS, state)),
        )

    return state


# --------------------------------------------------------------------------- #
# Ansible execution
# --------------------------------------------------------------------------- #


@dataclass
class PlaybookResult:
    playbook: str
    success: bool
    return_code: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float


def run_playbook(playbook_filename: str) -> PlaybookResult:
    """
    Executes an Ansible playbook via subprocess. Never raises -- every
    failure mode (missing file, missing binary, timeout, non-zero exit) is
    captured in the returned PlaybookResult so the webhook always responds
    cleanly.
    """
    playbook_path = os.path.join(PLAYBOOK_DIR, playbook_filename)
    start = time.monotonic()

    if not os.path.isfile(playbook_path):
        log_event("error", "Ansible playbook not found", playbook_path=playbook_path)
        return PlaybookResult(
            playbook=playbook_filename,
            success=False,
            return_code=None,
            stdout="",
            stderr=f"Playbook not found: {playbook_path}",
            duration_seconds=0.0,
        )

    cmd = ["ansible-playbook", playbook_path]
    if os.path.isfile(ANSIBLE_INVENTORY):
        cmd.extend(["-i", ANSIBLE_INVENTORY])

    log_event("info", "Dispatching Ansible playbook", command=" ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ANSIBLE_TIMEOUT_SECONDS,
            check=False,
        )
        duration = time.monotonic() - start
        success = proc.returncode == 0
        log_event(
            "info" if success else "error",
            "Ansible playbook execution finished",
            playbook=playbook_filename,
            return_code=proc.returncode,
            duration_seconds=round(duration, 2),
        )
        return PlaybookResult(
            playbook=playbook_filename,
            success=success,
            return_code=proc.returncode,
            stdout=proc.stdout[-4000:],
            stderr=proc.stderr[-4000:],
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        log_event(
            "error",
            "Ansible playbook timed out",
            playbook=playbook_filename,
            timeout_seconds=ANSIBLE_TIMEOUT_SECONDS,
        )
        return PlaybookResult(
            playbook=playbook_filename,
            success=False,
            return_code=None,
            stdout=(exc.stdout or ""),
            stderr=f"Timed out after {ANSIBLE_TIMEOUT_SECONDS}s",
            duration_seconds=duration,
        )
    except FileNotFoundError:
        log_event("error", "ansible-playbook executable not found on PATH")
        return PlaybookResult(
            playbook=playbook_filename,
            success=False,
            return_code=None,
            stdout="",
            stderr="ansible-playbook executable not found on PATH",
            duration_seconds=time.monotonic() - start,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("error", "Unexpected error running Ansible playbook", playbook=playbook_filename, error=str(exc))
        return PlaybookResult(
            playbook=playbook_filename,
            success=False,
            return_code=None,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - start,
        )


# --------------------------------------------------------------------------- #
# Flask app / webhook endpoint
# --------------------------------------------------------------------------- #

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return (
        jsonify(
            {
                "status": "ok",
                "model_loaded": model_handle.model is not None,
                "model_path": MODEL_PATH,
                "prometheus_url": PROMETHEUS_URL,
            }
        ),
        200,
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Alertmanager webhook receiver. Builds a fresh state vector from
    Prometheus, runs DQN inference, and dispatches the corresponding
    Ansible playbook (or skips cleanly on NO_ACTION).
    """
    received_at = datetime.now(timezone.utc).isoformat()

    try:
        alert_payload = request.get_json(silent=True) or {}
    except Exception as exc:  # noqa: BLE001
        log_event("error", "Failed to parse incoming webhook JSON", error=str(exc))
        alert_payload = {}

    alerts = alert_payload.get("alerts", [])
    alert_names = [a.get("labels", {}).get("alertname", "unknown") for a in alerts] if alerts else []

    log_event(
        "info",
        "Received Alertmanager webhook",
        received_at=received_at,
        alert_count=len(alerts),
        alert_names=alert_names,
    )

    state_vector = build_state_vector()
    action_id = model_handle.predict(state_vector)
    playbook_filename = ACTION_PLAYBOOK_MAP.get(action_id)
    action_label = ACTION_LABELS.get(action_id, "UNKNOWN")

    response_body = {
        "received_at": received_at,
        "alert_count": len(alerts),
        "alert_names": alert_names,
        "state_vector": dict(zip(STATE_KEYS, state_vector)),
        "action_id": action_id,
        "action_label": action_label,
        "playbook": playbook_filename,
    }

    if playbook_filename is None:
        log_event("info", "DQN selected NO_ACTION; no playbook dispatched", action_id=action_id)
        response_body["playbook_result"] = None
        return jsonify(response_body), 200

    result = run_playbook(playbook_filename)
    response_body["playbook_result"] = asdict(result)

    status_code = 200 if result.success else 502
    return jsonify(response_body), status_code


@app.route("/reload-model", methods=["POST"])
def reload_model():
    """Operational endpoint to hot-reload the model weights without restarting the process."""
    loaded = model_handle.load()
    return jsonify({"reloaded": loaded, "model_loaded": model_handle.model is not None}), (200 if loaded else 503)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    log_event(
        "info",
        "Starting intelligent_webhook",
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        model_path=MODEL_PATH,
        prometheus_url=PROMETHEUS_URL,
        playbook_dir=PLAYBOOK_DIR,
    )
    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT)