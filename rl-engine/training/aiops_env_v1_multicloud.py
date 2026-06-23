"""
multi_cloud_env.py

Custom Farama Gymnasium environment simulating a multi-cloud infrastructure
routing problem (AWS vs Azure) for Deep Q-Network (DQN) training.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import gymnasium as gym
import numpy as np
from gymnasium import spaces


class MultiCloudEnv(gym.Env):
    """
    A simulated multi-cloud environment where an agent decides, at each
    timestep, which cloud provider (AWS or Azure) should receive primary
    traffic, based on live infrastructure telemetry and spot pricing.

    Observation (Box, shape=(6,), dtype=float32):
        [0] CPU Utilization        -> [0.0, 1.0]
        [1] Memory Utilization     -> [0.0, 1.0]
        [2] Latency p95 (ms)       -> [0.0, 500.0]
        [3] Request Rate (req/s)   -> [0.0, 1000.0]
        [4] AWS Spot Price ($)     -> [0.05, 0.20]
        [5] Azure Spot Price ($)   -> [0.04, 0.16]

    Action (Discrete(2)):
        0 -> Route primary traffic to AWS
        1 -> Route primary traffic to Azure
    """

    metadata = {"render_modes": ["human"]}

    # --- Observation bounds -------------------------------------------------
    _CPU_LOW, _CPU_HIGH = 0.0, 1.0
    _MEM_LOW, _MEM_HIGH = 0.0, 1.0
    _LAT_LOW, _LAT_HIGH = 0.0, 500.0
    _RPS_LOW, _RPS_HIGH = 0.0, 1000.0
    _AWS_PRICE_LOW, _AWS_PRICE_HIGH = 0.05, 0.20
    _AZURE_PRICE_LOW, _AZURE_PRICE_HIGH = 0.04, 0.16

    # --- Simulation tuning knobs ---------------------------------------------
    _MAX_STEPS = 200
    _SLA_LATENCY_THRESHOLD_MS = 300.0
    _SLA_PENALTY = -5.0
    _LATENCY_COST_COEFF = 0.002

    # Price thresholds below which a provider is assumed to be reclaiming
    # spot capacity (resource crunch), simulating preemption pressure.
    _AWS_CRUNCH_PRICE_THRESHOLD = 0.08
    _AZURE_CRUNCH_PRICE_THRESHOLD = 0.06
    _CRUNCH_LATENCY_SPIKE_MS = 220.0

    # Random-walk step sizes
    _PRICE_WALK_STD = 0.005
    _CPU_WALK_STD = 0.03
    _MEM_WALK_STD = 0.03
    _RPS_WALK_STD = 20.0

    def __init__(self, render_mode: Optional[str] = None) -> None:
        super().__init__()

        self.render_mode = render_mode

        low = np.array(
            [
                self._CPU_LOW,
                self._MEM_LOW,
                self._LAT_LOW,
                self._RPS_LOW,
                self._AWS_PRICE_LOW,
                self._AZURE_PRICE_LOW,
            ],
            dtype=np.float32,
        )
        high = np.array(
            [
                self._CPU_HIGH,
                self._MEM_HIGH,
                self._LAT_HIGH,
                self._RPS_HIGH,
                self._AWS_PRICE_HIGH,
                self._AZURE_PRICE_HIGH,
            ],
            dtype=np.float32,
        )

        self.observation_space = spaces.Box(low=low, high=high, dtype=np.float32)
        self.action_space = spaces.Discrete(2)

        self._current_step: int = 0

        # State variables (initialized properly in reset())
        self.cpu_util: float = 0.0
        self.mem_util: float = 0.0
        self.latency_p95: float = 0.0
        self.request_rate: float = 0.0
        self.aws_price: float = 0.0
        self.azure_price: float = 0.0

    # -------------------------------------------------------------------- #
    # Core Gym API
    # -------------------------------------------------------------------- #
    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        super().reset(seed=seed)

        self._current_step = 0

        # Sensible baseline averages
        self.cpu_util = 0.50
        self.mem_util = 0.55
        self.latency_p95 = 50.0
        self.request_rate = 500.0
        self.aws_price = (self._AWS_PRICE_LOW + self._AWS_PRICE_HIGH) / 2.0
        self.azure_price = (self._AZURE_PRICE_LOW + self._AZURE_PRICE_HIGH) / 2.0

        observation = self._get_obs()
        info: Dict[str, Any] = {}
        return observation, info

    def step(
        self, action: int
    ) -> Tuple[np.ndarray, float, bool, bool, Dict[str, Any]]:
        assert self.action_space.contains(action), f"Invalid action: {action}"

        self._current_step += 1

        # 1. Random-walk update of simulated spot prices
        self.aws_price = float(
            np.clip(
                self.aws_price + self.np_random.normal(0.0, self._PRICE_WALK_STD),
                self._AWS_PRICE_LOW,
                self._AWS_PRICE_HIGH,
            )
        )
        self.azure_price = float(
            np.clip(
                self.azure_price + self.np_random.normal(0.0, self._PRICE_WALK_STD),
                self._AZURE_PRICE_LOW,
                self._AZURE_PRICE_HIGH,
            )
        )

        # 2. Random-walk update of organic infrastructure telemetry
        self.cpu_util = float(
            np.clip(
                self.cpu_util + self.np_random.normal(0.0, self._CPU_WALK_STD),
                self._CPU_LOW,
                self._CPU_HIGH,
            )
        )
        self.mem_util = float(
            np.clip(
                self.mem_util + self.np_random.normal(0.0, self._MEM_WALK_STD),
                self._MEM_LOW,
                self._MEM_HIGH,
            )
        )
        self.request_rate = float(
            np.clip(
                self.request_rate + self.np_random.normal(0.0, self._RPS_WALK_STD),
                self._RPS_LOW,
                self._RPS_HIGH,
            )
        )

        # 3. Synthetic latency calculation based on chosen action
        base_latency = 20.0 + (self.cpu_util * 150.0) + (self.mem_util * 80.0)
        base_latency += (self.request_rate / self._RPS_HIGH) * 60.0

        is_resource_crunch = False
        if action == 0 and self.aws_price < self._AWS_CRUNCH_PRICE_THRESHOLD:
            # AWS spot price dropped too low -> capacity reclamation pressure
            is_resource_crunch = True
        elif action == 1 and self.azure_price < self._AZURE_CRUNCH_PRICE_THRESHOLD:
            # Azure spot price dropped too low -> capacity reclamation pressure
            is_resource_crunch = True

        if is_resource_crunch:
            base_latency += self._CRUNCH_LATENCY_SPIKE_MS + self.np_random.uniform(
                0.0, 30.0
            )

        self.latency_p95 = float(
            np.clip(base_latency, self._LAT_LOW, self._LAT_HIGH)
        )

        # 4. Reward function
        current_provider_cost = self.aws_price if action == 0 else self.azure_price
        reward = -current_provider_cost - (
            self._LATENCY_COST_COEFF * self.latency_p95
        )

        if self.latency_p95 > self._SLA_LATENCY_THRESHOLD_MS:
            reward += self._SLA_PENALTY

        # 5. Termination / truncation
        terminated = False
        truncated = self._current_step >= self._MAX_STEPS

        observation = self._get_obs()
        info: Dict[str, Any] = {
            "resource_crunch": is_resource_crunch,
            "provider": "AWS" if action == 0 else "Azure",
            "provider_cost": current_provider_cost,
        }

        return observation, reward, terminated, truncated, info

    def render(self) -> None:
        if self.render_mode == "human":
            print(
                f"Step {self._current_step:03d} | "
                f"CPU={self.cpu_util:.2f} MEM={self.mem_util:.2f} "
                f"Latency={self.latency_p95:.1f}ms RPS={self.request_rate:.1f} "
                f"AWS=${self.aws_price:.4f} Azure=${self.azure_price:.4f}"
            )

    def close(self) -> None:
        pass

    # -------------------------------------------------------------------- #
    # Helpers
    # -------------------------------------------------------------------- #
    def _get_obs(self) -> np.ndarray:
        return np.array(
            [
                self.cpu_util,
                self.mem_util,
                self.latency_p95,
                self.request_rate,
                self.aws_price,
                self.azure_price,
            ],
            dtype=np.float32,
        )
