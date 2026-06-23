"""
train_dqn.py

Trains a Deep Q-Network (DQN) agent inside the custom MultiCloudEnv
gymnasium environment to learn optimal multi-cloud traffic routing.
"""

from __future__ import annotations

import random
from collections import deque, namedtuple
from typing import Deque, List, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from multi_cloud_env import MultiCloudEnv

# --------------------------------------------------------------------------- #
# Hyperparameters
# --------------------------------------------------------------------------- #
NUM_EPISODES = 100
GAMMA = 0.99
LEARNING_RATE = 0.001
BATCH_SIZE = 64
REPLAY_BUFFER_CAPACITY = 10_000

EPSILON_START = 1.0
EPSILON_MIN = 0.01
EPSILON_DECAY = 0.995

TARGET_UPDATE_EVERY_EPISODES = 10  # periodic hard update for stable targets
LOG_EVERY_EPISODES = 10

WEIGHTS_SAVE_PATH = "multi_cloud_dqn.pth"

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

Transition = namedtuple(
    "Transition", ("state", "action", "reward", "next_state", "done")
)


# --------------------------------------------------------------------------- #
# Q-Network
# --------------------------------------------------------------------------- #
class QNetwork(nn.Module):
    """Feed-forward network: 6 inputs -> 64 -> 64 -> 2 outputs."""

    def __init__(self, input_dim: int, output_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, output_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


# --------------------------------------------------------------------------- #
# Replay Buffer
# --------------------------------------------------------------------------- #
class ReplayBuffer:
    def __init__(self, capacity: int) -> None:
        self.buffer: Deque[Transition] = deque(maxlen=capacity)

    def push(
        self,
        state: np.ndarray,
        action: int,
        reward: float,
        next_state: np.ndarray,
        done: bool,
    ) -> None:
        self.buffer.append(Transition(state, action, reward, next_state, done))

    def sample(self, batch_size: int) -> List[Transition]:
        return random.sample(self.buffer, batch_size)

    def __len__(self) -> int:
        return len(self.buffer)


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class DQNAgent:
    def __init__(self, state_dim: int, action_dim: int) -> None:
        self.action_dim = action_dim

        self.policy_net = QNetwork(state_dim, action_dim).to(DEVICE)
        self.target_net = QNetwork(state_dim, action_dim).to(DEVICE)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()

        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=LEARNING_RATE)
        self.loss_fn = nn.MSELoss()

        self.replay_buffer = ReplayBuffer(REPLAY_BUFFER_CAPACITY)
        self.epsilon = EPSILON_START

    def select_action(self, state: np.ndarray) -> int:
        if random.random() < self.epsilon:
            return random.randrange(self.action_dim)

        with torch.no_grad():
            state_tensor = torch.as_tensor(
                state, dtype=torch.float32, device=DEVICE
            ).unsqueeze(0)
            q_values = self.policy_net(state_tensor)
            return int(torch.argmax(q_values, dim=1).item())

    def decay_epsilon(self) -> None:
        self.epsilon = max(EPSILON_MIN, self.epsilon * EPSILON_DECAY)

    def update_target_network(self) -> None:
        self.target_net.load_state_dict(self.policy_net.state_dict())

    def optimize(self) -> None:
        if len(self.replay_buffer) < BATCH_SIZE:
            return

        batch = self.replay_buffer.sample(BATCH_SIZE)

        states = torch.as_tensor(
            np.array([t.state for t in batch]), dtype=torch.float32, device=DEVICE
        )
        actions = torch.as_tensor(
            [t.action for t in batch], dtype=torch.int64, device=DEVICE
        ).unsqueeze(1)
        rewards = torch.as_tensor(
            [t.reward for t in batch], dtype=torch.float32, device=DEVICE
        ).unsqueeze(1)
        next_states = torch.as_tensor(
            np.array([t.next_state for t in batch]),
            dtype=torch.float32,
            device=DEVICE,
        )
        dones = torch.as_tensor(
            [t.done for t in batch], dtype=torch.float32, device=DEVICE
        ).unsqueeze(1)

        # Current Q estimates for the actions taken
        current_q_values = self.policy_net(states).gather(1, actions)

        # Target Q values from the target network
        with torch.no_grad():
            max_next_q_values = self.target_net(next_states).max(dim=1, keepdim=True)[0]
            target_q_values = rewards + GAMMA * max_next_q_values * (1.0 - dones)

        loss = self.loss_fn(current_q_values, target_q_values)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()


# --------------------------------------------------------------------------- #
# Training Loop
# --------------------------------------------------------------------------- #
def train() -> None:
    env = MultiCloudEnv()

    state_dim = env.observation_space.shape[0]
    action_dim = env.action_space.n

    agent = DQNAgent(state_dim, action_dim)

    episode_rewards: List[float] = []

    for episode in range(1, NUM_EPISODES + 1):
        state, _ = env.reset()
        total_reward = 0.0
        truncated = False
        terminated = False

        while not (truncated or terminated):
            action = agent.select_action(state)
            next_state, reward, terminated, truncated, _ = env.step(action)

            agent.replay_buffer.push(state, action, reward, next_state, terminated)
            agent.optimize()

            state = next_state
            total_reward += reward

        agent.decay_epsilon()
        episode_rewards.append(total_reward)

        if episode % TARGET_UPDATE_EVERY_EPISODES == 0:
            agent.update_target_network()

        if episode % LOG_EVERY_EPISODES == 0:
            avg_reward = sum(episode_rewards[-LOG_EVERY_EPISODES:]) / LOG_EVERY_EPISODES
            print(
                f"Episode {episode:4d}/{NUM_EPISODES} | "
                f"Total Reward: {total_reward:8.2f} | "
                f"Avg(last {LOG_EVERY_EPISODES}): {avg_reward:8.2f} | "
                f"Epsilon: {agent.epsilon:.3f}"
            )

    torch.save(agent.policy_net.state_dict(), WEIGHTS_SAVE_PATH)
    print(f"\nTraining complete. Weights saved to '{WEIGHTS_SAVE_PATH}'.")

    env.close()


if __name__ == "__main__":
    train()
