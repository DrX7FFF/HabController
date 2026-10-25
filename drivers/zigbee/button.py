"""Bouton ou télécommande Zigbee : relaie ses événements (sur pile, base : sensor.py).

État : {"connected": bool, "battery": %} ; les appuis ne sont pas un état mais des événements,
transmis tels que le correctif zha-quirks de l'appareil les nomme, avec leur valeur éventuelle :
Xiaomi b1acn01 : single, double, hold, release ; Xiaomi rond : click (valeur : type d'appui) ;
cube : flip, rotate_right… (valeur : détails, dont l'angle) ; Hue Dimmer : on_press,
off_long_press…

    from drivers.zigbee.button import Button

    bouton_lit = Button()

Dans une automatisation : @on_event("bouton_lit.double"), ou @on_event("bouton_lit") pour tous.
"""

import logging

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

    def zha_send_event(self, action: str, args) -> None:
        """Appelé par le correctif zha-quirks de l'appareil pour chaque appui ou geste."""
        self._received()
        log.debug("%s : événement %s %s", self.id, action, args)
        self._emit(action, args or None)


Driver = Button  # convention : le chargeur cherche ce nom
