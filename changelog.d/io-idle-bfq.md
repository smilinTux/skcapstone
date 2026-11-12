### Fixed

- `scripts/install.sh` now installs `systemd/udev/60-ioscheduler-bfq.rules` (bfq on spinning disks) and `/etc/modules-load.d/bfq.conf`. The syncthing drop-in and `skmemory-sync@` set `IOSchedulingClass=idle`, which the kernel ignores unless the disk uses bfq. noroc2027 (QEMU HDD, scheduler `none`) sat at ~90% IO pressure and load 35 on 4 cores with both jobs "idle" on paper.
- `skcapstone doctor` adds `io:scheduler:<disk>` (spinning disk not on bfq/cfq) and `io:syncthing_dropin` (syncthing installed without `syncthing.service.d/nice.conf`, which was the case on noroc2027).
