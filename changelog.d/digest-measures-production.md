Fleet digest now counts workers as systemd units (it counted migration-era tmux
sessions and reported zero while eight ran) and alerts on workers that are alive
but producing nothing, measured as workspace silence.
