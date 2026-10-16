import random

import httpx
import pytest

from ronda_agent.api import Api
from ronda_agent.buffer import Buffer
from ronda_agent.config import StateStore
from ronda_agent.main import Agent
from tests.fakes import Clock, FakeCollectors, FakeServer


@pytest.fixture
def env(tmp_path):
    (tmp_path / "config.toml").write_text(
        'server_url = "https://ronda.test"\nenrollment_key = "IT-abcdefgh"\n')
    clock, server = Clock(), FakeServer()
    http = httpx.Client(base_url="https://ronda.test", transport=httpx.MockTransport(server))

    def make_agent():
        return Agent(tmp_path, Api(http), FakeCollectors(clock), Buffer(tmp_path / "buffer.db"),
                     StateStore(tmp_path / "state.dat"), clock=clock.mono, now=clock.now, rng=random.Random(7))

    return tmp_path, clock, server, make_agent
