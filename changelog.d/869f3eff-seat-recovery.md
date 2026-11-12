- Card `869f3eff`: recover stopped, process-free failed governed oneshots with
  `systemctl reset-failed`; previously their retained failed state permanently
  fenced every later lifecycle-seat generation.
