# Pi Wi-Fi scanner

This is sample code to test deployment and builds in Armainer. It is not an application to keep. When a build reaches the machine, switch the fleet to your own repository.

The sample scans nearby Wi-Fi networks from a Raspberry Pi and shows them on a web page. A scan runs when the container starts, then about once a minute. The page stores the results in Postgres. Clear, at the bottom of the page, deletes the stored networks. The Pi stays connected to its own network.

The scanner reads `wlan0` and does not change the association.

## Board families

Use this sample only on a Raspberry Pi fleet that has a Wi-Fi interface named `wlan0`:

- Raspberry Pi 3
- Raspberry Pi 4, Pi 400, and Pi 5
- Compute Module 4 or Compute Module 5 only when that module has a Wi-Fi radio

Do not use this sample on these fleets. The radio and the interface name are different, and this program has not been tested on that hardware:

- Orange Pi 5, 5B, 5 Plus, and 5 Pro
- Banana Pi BPI-M5
- Generic x86 mini PC

For those fleets, start with [Armainer/sample-hello](https://github.com/Armainer/sample-hello).

## Run it

In a Raspberry Pi fleet, build `https://github.com/Armainer/sample-pi-wifi-scanner.git` on branch `main`.

Open `http://<pi-address>:8080/`. If the fleet firewall is on, allow TCP port 8080 on the Security tab.

The database password in `docker-compose.yml` is the sample password `wifi-survey`. Anyone who can read this repository can see it. Change it before you use this project for anything other than a trial.

## Licence

MIT. You can use, change, and ship this sample, including in your own product. Keep the copyright notice from `LICENSE` in copies of the code.
