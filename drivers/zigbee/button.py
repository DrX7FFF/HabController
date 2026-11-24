"""Pilote bouton Zigbee."""

import logging
from typing import override

from core.base import EventAction, EventSource
from drivers.zigbee import ERRORS, MANUFACTURER, clusters, find_cluster
from drivers.zigbee.sensor import Sensor

log = logging.getLogger(__name__)


class Button(Sensor):
    event_action = EventSource[EventAction]()  # action : nommée par le correctif zha-quirks

    @override
    def _attach(self, app) -> None:
        super()._attach(app)
        if self.zdev is None:
            return
        for cluster in clusters(self.zdev):
            cluster.add_listener(self)
            self._unsubscribe.append(lambda cluster=cluster: cluster.remove_listener(self))

    @override
    async def _configure(self, zdev) -> None:
        await super()._configure(zdev)
        # appuis envoyés par le cluster du fabricant (ex. Philips), aux seuls destinataires liés
        if (cluster := find_cluster(zdev, MANUFACTURER)) is not None:
            try:
                await cluster.bind()
            except ERRORS as err:
                log.warning("%s : binding de 0x%04x refusé (%s)", self.id, MANUFACTURER, err)

    def zha_send_event(self, action: str, args) -> None:
        """Appelé par le correctif zha-quirks de l'appareil pour chaque appui ou geste."""
        self._received()
        log.debug("%s : événement %s %s", self.id, action, args)
        self._emit(action, args or None)


Driver = Button  # convention : le chargeur cherche ce nom
