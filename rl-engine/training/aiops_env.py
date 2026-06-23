from __future__ import annotations

import gymnasium as gym
import numpy as np
from gymnasium import spaces


class AIOpsEnv(gym.Env):

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
            observation = np.array(
                [0, 1, 0.4, 0.4, 0.3, 1],
                dtype=np.float32,
            )

        elif self.incident_type == 1:
            observation = np.array(
                [1, 0, 0.4, 0.4, 0.3, 1],
                dtype=np.float32,
            )

        elif self.incident_type == 2:
            observation = np.array(
                [0, 0, 0.7, 0.7, 0.8, 2],
                dtype=np.float32,
            )

        else:
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
