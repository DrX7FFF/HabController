# HabController
Habitation domotique home mad

## Start
./habctrl

## Pairing HomeKit
```batch
.venv/bin/python pair.py fp2
```


## détection clé USB SonOff Dongle Plus MG24 
❯ lsusb | grep -i "Silicon"
Bus 001 Device 015: ID 10c4:ea60 Silicon Labs CP210x UART Bridge

❯ ls -l /dev/serial/by-id/
total 0
lrwxrwxrwx 1 root root 13 oct.  10 23:08 usb-SONOFF_SONOFF_Dongle_Plus_MG24_cccd71bbb69aef1193e6b89061ce3355-if00-port0 -> ../../ttyUSB0
