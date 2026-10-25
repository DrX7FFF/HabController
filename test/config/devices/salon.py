from drivers.homekit.fp2 import FP2
from drivers.yeelight.bulb import YeelightBulb
from drivers.zigbee.plug import Plug

salon = YeelightBulb(host="192.0.2.10")
prise_salon = Plug()
fp2 = FP2(zonemapping={0: "Global", 1: "salon", 2: "cuisine"})
