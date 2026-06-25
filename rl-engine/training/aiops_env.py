from __future__ import annotations

import gymnasium as gym
import numpy as np
from gymnasium import spaces


class AIOpsEnv(gym.Env):
    """
    Simulated AIOps autonomous remediation environment for DQN training.

    Observation space (Box, shape (6,), dtype=float32):
        0: web_target_up      - 1.0 if the web target (nginx) is reachable, else 0.0
        1: data_target_up     - 1.0 if the data target (node_exporter) is reachable, else 0.0
        2: cpu_utilization    - normalized CPU utilization, 0.0-1.0
        3: memory_utilization - normalized memory utilization, 0.0-1.0
        4: spot_exporter_up   - spot price exporter health/responsiveness score, 0.0-1.0
        5: system_load        - bucketed system load / incident severity score, 0-3

    Action space (Discrete(4)):
        0: Restart Nginx (web target)
        1: Restart Node Exporter (data target)
        2: Restart Both Services
        3: No Action
    """

    def __init__(self):
        super().__init__()

        self.observation_space = spaces.Box(
            low=np.array([0, 0, 0, 0, 0, 0], dtype=np.float32),
            high=np.array([1, 1, 1, 1, 1, 3], dtype=np.float32),
            dtype=np.float32,
        )

        self.action_space = spaces.Discrete(4)

        self.incident_type = 0
        self.current_state = None

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        self.incident_type = np.random.randint(0, 4)

        if self.incident_type == 0:
            # web_target_up=0, data_target_up=1 -> correct action is Restart Nginx (0)
            observation = np.array(
                [0, 1, 0.4, 0.4, 0.3, 1],
                dtype=np.float32,
            )

        elif self.incident_type == 1:
            # web_target_up=1, data_target_up=0 -> correct action is Restart Node Exporter (1)
            observation = np.array(
                [1, 0, 0.4, 0.4, 0.3, 1],
                dtype=np.float32,
            )

        elif self.incident_type == 2:
            # both targets down, elevated cpu/memory/system_load -> correct action is Restart Both (2)
            observation = np.array(
                [0, 0, 0.7, 0.7, 0.8, 2],
                dtype=np.float32,
            )

        else:
            # both targets up, nominal load -> correct action is No Action (3)
            observation = np.array(
                [1, 1, 0.3, 0.4, 0.2, 0],
                dtype=np.float32,
            )

        self.current_state = observation

        return observation, {}

    def step(self, action):

        reward = -20.0

        if self.incident_type == 0 and action == 0:
            reward = 20.0

        elif self.incident_type == 1 and action == 1:
            reward = 20.0

        elif self.incident_type == 2 and action == 2:
            reward = 20.0

        elif self.incident_type == 3 and action == 3:
            reward = 10.0

        observation = self.current_state

        terminated = True
        truncated = False

        return observation, reward, terminated, truncated, {}