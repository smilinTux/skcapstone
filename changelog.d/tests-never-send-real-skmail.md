The test suite never mails a real mailbox: SKMAIL_BIN defaults to /bin/true for every test, so fixture dispatch events no longer reach jarvis when tests run on a fleet host.
