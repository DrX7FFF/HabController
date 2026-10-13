"""Test : journalise les changements d'état du salon."""

import logging

from core.automation import on_change
from devices.salon import salon

log = logging.getLogger(__name__)


@on_change("salon.power")
async def test(event):
    log.debug("salon = %s %s", event.new, salon.brightness)
