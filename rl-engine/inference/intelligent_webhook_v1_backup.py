"""
intelligent_webhook.py

Flask webhook listener that receives Alertmanager alerts, queries live
Prometheus metrics, runs inference through a trained DQN model, and
triggers the corresponding Ansible playbook to route traffic to the
optimal cloud provider.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

import numpy as np
import requests
import torch
import torch.nn as nn
from flask import Flask, jsonify, request

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
PROMETHEUS_URL = "http://localhost:9090/api/v1/query"
PROMETHEUS_TIMEOUT_SECONDS = 5

MODEL_WEIGHTS_PATH = "multi_cloud_dqn.pth"
INVENTORY_PATH = "inventory.ini"
PLAYBOOK_AWS = "route_aws.yml"
PLAYBOOK_AZURE = "route_azure.yml"
ANSIBLE_TIMEOUT_SECONDS = 60

STATE_DIM = 6
ACTION_DIM = 2
HIDDEN_UNITS = 64

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Fallback defaults if a Prometheus query fails or returns no data,
# mirroring the baseline averages used during training.
FALLBACK_DEFAULTS = {
    "cpu_util": 0.50,
    "mem_util": 0.55,
    "latency_p95": 50.0,
    "request_rate": 500.0,
    "aws_spot_price": 0.125,
    "azure_spot_price": 0.10,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("intelligent_webhook")

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Q-Network (must match training architecture exactly)
# --------------------------------------------------------------------------- #
class QNetwork(nn.Module):
    """Feed-forward network: 6 inputs -> 64 -> 64 -> 2 outputs."""

    def __init__(self, input_dim: int, output_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, HIDDEN_UNITS),
            nn.ReLU(),
            nn.Linear(HIDDEN_UNITS, HIDDEN_UNITS),
            nn.ReLU(),
            nn.Linear(HIDDEN_UNITS, output_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def load_model(weights_path: str) -> QNetwork:
    model = QNetwork(STATE_DIM, ACTION_DIM).to(DEVICE)
    state_dict = torch.load(weights_path, map_location=DEVICE)
    model.load_state_dict(state_dict)
    model.eval()
    logger.info("Loaded DQN model weights from '%s'.", weights_path)
    return model


model = load_model(MODEL_WEIGHTS_PATH)


# --------------------------------------------------------------------------- #
# Prometheus querying
# --------------------------------------------------------------------------- #
def query_prometheus(promql: str) -> Optional[float]:
    """
    Executes an instant PromQL query and returns the scalar value of the
    first result, or None if the query fails / returns no data.
    """
    try:
        response = requests.get(
            PROMETHEUS_URL,
            params={"query": promql},
            timeout=PROMETHEUS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("status") != "success":
            logger.warning("Prometheus query failed (status): %s", promql)
            return None

        results = payload.get("data", {}).get("result", [])
        if not results:
            logger.warning("Prometheus query returned no data: %s", promql)
            return None

        # Instant vector result format: [timestamp, "value_as_string"]
        raw_value = results[0]["value"][1]
        return float(raw_value)

    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        logger.error("Error querying Prometheus ('%s'): %s", promql, exc)
        return None


def fetch_current_state() -> np.ndarray:
    """
    Queries Prometheus for the 6 metrics that make up the model's
    observation vector, falling back to safe defaults on any failure.
    """
    cpu_util = query_prometheus("avg(cpu_utilization)")
    mem_util = query_prometheus("avg(memory_utilization)")
    latency_p95 = query_prometheus("avg(network_latency_p95)")
    request_rate = query_prometheus("sum(request_rate)")
    aws_spot_price = query_prometheus('infra_spot_price{provider="aws"}')
    azure_spot_price = query_prometheus('infra_spot_price{provider="azure"}')

    resolved = {
        "cpu_util": cpu_util if cpu_util is not None else FALLBACK_DEFAULTS["cpu_util"],
        "mem_util": mem_util if mem_util is not None else FALLBACK_DEFAULTS["mem_util"],
        "latency_p95": latency_p95
        if latency_p95 is not None
        else FALLBACK_DEFAULTS["latency_p95"],
        "request_rate": request_rate
        if request_rate is not None
        else FALLBACK_DEFAULTS["request_rate"],
        "aws_spot_price": aws_spot_price
        if aws_spot_price is not None
        else FALLBACK_DEFAULTS["aws_spot_price"],
        "azure_spot_price": azure_spot_price
        if azure_spot_price is not None
        else FALLBACK_DEFAULTS["azure_spot_price"],
    }

    for key, value in resolved.items():
        if locals().get(key.split("_")[0], None) is None:
            pass  # defaults already logged individually below

    logger.info("Resolved state vector: %s", resolved)

    state = np.array(
        [
            resolved["cpu_util"],
            resolved["mem_util"],
            resolved["latency_p95"],
            resolved["request_rate"],
            resolved["aws_spot_price"],
            resolved["azure_spot_price"],
        ],
        dtype=np.float32,
    ).reshape(1, STATE_DIM)

    return state


# --------------------------------------------------------------------------- #
# Inference
# --------------------------------------------------------------------------- #
def predict_action(state: np.ndarray) -> int:
    state_tensor = torch.as_tensor(state, dtype=torch.float32, device=DEVICE)
    with torch.no_grad():
        q_values = model(state_tensor)
        action = q_values.argmax(dim=1).item()
    logger.info("Q-values: %s -> selected action: %d", q_values.tolist(), action)
    return action


# --------------------------------------------------------------------------- #
# Ansible execution
# --------------------------------------------------------------------------- #
def run_ansible_playbook(playbook: str) -> bool:
    command = ["ansible-playbook", "-i", INVENTORY_PATH, playbook]
    logger.info("Executing: %s", " ".join(command))

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=ANSIBLE_TIMEOUT_SECONDS,
            check=False,
        )

        if result.returncode == 0:
            logger.info("Playbook '%s' executed successfully.", playbook)
            logger.debug("stdout: %s", result.stdout)
            return True

        logger.error(
            "Playbook '%s' failed with exit code %d.\nstdout: %s\nstderr: %s",
            playbook,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return False

    except subprocess.TimeoutExpired:
        logger.error("Playbook '%s' timed out after %ds.", playbook, ANSIBLE_TIMEOUT_SECONDS)
        return False
    except FileNotFoundError:
        logger.error("ansible-playbook executable not found on PATH.")
        return False


def route_traffic(action: int) -> bool:
    if action == 0:
        return run_ansible_playbook(PLAYBOOK_AWS)
    return run_ansible_playbook(PLAYBOOK_AZURE)


# --------------------------------------------------------------------------- #
# Flask route
# --------------------------------------------------------------------------- #
@app.route("/webhook", methods=["POST"])
def webhook() -> tuple:
    payload = request.get_json(silent=True)

    if payload is None:
        logger.warning("Received webhook with invalid or missing JSON body.")
        return jsonify({"error": "invalid or missing JSON payload"}), 400

    alert_names = [
        alert.get("labels", {}).get("alertname", "unknown")
        for alert in payload.get("alerts", [])
    ]
    logger.info("Received Alertmanager webhook. Alerts: %s", alert_names)

    state = fetch_current_state()
    action = predict_action(state)
    success = route_traffic(action)

    provider = "aws" if action == 0 else "azure"

    return jsonify(
        {
            "status": "ok" if success else "playbook_failed",
            "action": action,
            "provider": provider,
            "state_vector": state.tolist(),
        }
    ), (200 if success else 500)


@app.route("/health", methods=["GET"])
def health() -> tuple:
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
