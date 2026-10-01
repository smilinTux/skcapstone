Production drift checks now verify the single native-interpreter script in
`.skenv/bin` and its exact `.local/bin` compatibility shim. Legacy layout checks
remain unchanged. Checked-in `systemd/production` templates define the serialized
seat cycle and shared-policy service environment without fixed model IDs.
