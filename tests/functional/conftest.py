import asyncio
import logging

import pytest

log = logging.getLogger(__name__)


class Helper:
    """Helper class for async functions."""

    @staticmethod
    async def run_action_wait(unit, action_name, **kwargs):
        action = await unit.run_action(action_name, **kwargs)
        await action.wait()
        return action.results

    @staticmethod
    async def run_wait(unit, command):
        action = await unit.run(command)
        await action.wait()
        return action.results


def pytest_collection_modifyitems(items):
    """Exempt the functional suite from the repo-wide -Werror setting.

    pyproject.toml's [tool.pytest.ini_options] filterwarnings promotes warnings
    to errors, which is what we want for the unit tests. That setting is
    repo-wide, though, and this suite also runs under pytest (tox -e func), so
    without this the functional tests would fail on deprecations raised deep in
    juju/libjuju/zaza rather than on anything this charm controls.
    """
    for item in items:
        item.add_marker(pytest.mark.filterwarnings("default"))


def pytest_addoption(parser):
    parser.addoption(
        "--series",
        type=str,
        default="jammy",
        help="Set the series for the machine units.",
    )


@pytest.fixture
def series(request):
    return request.config.getoption("--series")


@pytest.fixture
def apt_mirror_app(ops_test):
    return ops_test.model.applications["apt-mirror"]


@pytest.fixture
def apt_mirror_unit(apt_mirror_app):
    return apt_mirror_app.units[0]


@pytest.fixture
def configs(apt_mirror_app):
    async def get_config_synced():
        return await apt_mirror_app.get_config()

    loop = asyncio.get_event_loop()
    coroutine = get_config_synced()
    return loop.run_until_complete(coroutine)


@pytest.fixture(scope="class")
def helper():
    return Helper


@pytest.fixture
def base_path(configs):
    return configs.get("base-path").get("value")
