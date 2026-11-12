- Card `a4827adf`: staged rollout gates the requested remote checkout, writes
  canonical production compatibility shims, and installs matching main systemd
  units while retaining sknoded host settings and unit preimages. Unit installation
  reloads systemd definitions without enabling or starting services.
