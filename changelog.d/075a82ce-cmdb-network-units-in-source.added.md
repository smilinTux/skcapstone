The CMDB network reconcile now ships complete in source: the
`skcapstone-cmdb-network {apply|shadow}` runner, the owner launcher template
`data/cmdb-network-apply.sh`, the write-free shadow unit and the
`skcapstone-cmdb-reconcile-network.timer` (01/3:47, never overlapping the legacy
00/3:17 local timer). Node targets live in
`~/.config/skcapstone/cmdb-network-targets`. Nothing installs or enables the
timer; that is a gate 4 cutover step (card 075a82ce).
