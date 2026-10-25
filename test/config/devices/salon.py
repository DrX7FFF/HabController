from drivers.all import FP2, Plug, YeelightBulb

salon = YeelightBulb(host="192.0.2.10")
prise_salon = Plug(ieee="a4:c1:38:d6:d7:c6:db:f6")
fp2 = FP2(zonemapping={0: "Global", 1: "salon", 2: "cuisine"})
