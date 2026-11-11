"""Shared builders for the TestBoard_ADC124 tests."""

from captest import runner
from captest.boards import load_driver
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.simbase import SimLink

BOARD = "TestBoard_ADC124"


def driver():
    return load_driver(BOARD)


def make_session(settings=None, clock=None, **config):
    """(session, device, clock) on a simulated ADC124 with the given simulator config."""
    clock = clock or VirtualClock()
    settings = settings or Settings.for_board(BOARD)
    drv = driver()
    device = drv.create_simulator(clock, settings.bias, drv.sim_config(**config))
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock,
                                        simulated=True, driver=drv)
    return session, device, clock


def burst_commands(device):
    return [c for c in device.commands if c.startswith("capburst")]
