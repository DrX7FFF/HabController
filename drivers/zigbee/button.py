"""Pilote bouton Zigbee."""

import logging

from drivers.zigbee import ERRORS, MANUFACTURER, find_cluster
from drivers.zigbee.sensor import Sensor

log = logging.getLogger(__name__)


class Button(Sensor):
    def _attach(self, app) -> None:
        super()._attach(app)
        if self.zdev is None:
            return
        for endpoint_id, endpoint in self.zdev.endpoints.items():
            if endpoint_id == 0:
                continue
            for cluster in (*endpoint.in_clusters.values(), *endpoint.out_clusters.values()):
                cluster.add_listener(self)
                self._unsubscribe.append(lambda cluster=cluster: cluster.remove_listener(self))

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
