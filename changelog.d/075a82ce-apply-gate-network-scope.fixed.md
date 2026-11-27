The governed CMDB apply gate now counts only network-scope artifacts. It took
the three newest complete artifacts of any scope, so any local timer run
between the shadows and the apply made it refuse (gate 4 blocker B2, card
075a82ce).
