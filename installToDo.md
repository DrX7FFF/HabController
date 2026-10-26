## Phase 9 – Service systemd
```bash
sudo tee /etc/systemd/system/habctrl.service > /dev/null <<EOF
[Unit]
Description=HabController
Wants=network-online.target
After=network-online.target home-habadm-HabController-temp.mount
# au-delà de 5 démarrages en 5 min (code qui plante dès le lancement) : arrêt définitif
StartLimitIntervalSec=300
StartLimitBurst=5
RequiresMountsFor=/home/habadm/HabController/temp

[Service]
User=habadm
Group=habadm
SupplementaryGroups=dialout
WorkingDirectory=/home/habadm/HabController
ExecStart=/home/habadm/HabController/habctrl
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now habctrl
```
Vérification :
```bash
systemctl status habctrl
journalctl -u habctrl -f
```
Redémarrer le serveur et vérifier que tout repart :
```bash
sudo reboot
```
